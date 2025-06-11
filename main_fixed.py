import os
import logging
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from tradovate_api import TradovateClient
import uvicorn
import httpx
import json
import hashlib
import asyncio

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
logging.info(f"Loaded WEBHOOK_SECRET: {WEBHOOK_SECRET}")

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, "webhook_trades.log")
logging.basicConfig(
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ],
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = FastAPI()
client = TradovateClient()
recent_alert_hashes = set()
MAX_HASHES = 20  # Keep the last 20 unique alerts

@app.on_event("startup")
async def startup_event():
    await client.authenticate()

async def get_latest_price(symbol: str):
    url = f"https://demo-api.tradovate.com/v1/marketdata/quote/{symbol}"
    headers = {"Authorization": f"Bearer {client.access_token}"}
    async with httpx.AsyncClient() as http_client:
        response = await http_client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data["last"]

async def cancel_all_orders(symbol):
    url = f"https://demo-api.tradovate.com/v1/order/cancelallorders"
    headers = {"Authorization": f"Bearer {client.access_token}"}
    async with httpx.AsyncClient() as http_client:
        await http_client.post(url, headers=headers, json={"symbol": symbol})

async def flatten_position(symbol):
    url = f"https://demo-api.tradovate.com/v1/position/closeposition"
    headers = {"Authorization": f"Bearer {client.access_token}"}
    async with httpx.AsyncClient() as http_client:
        await http_client.post(url, headers=headers, json={"symbol": symbol})

async def wait_until_no_open_orders(symbol, timeout=10):
    """
    Poll Tradovate until there are no open orders for the symbol, or until timeout (seconds).
    """
    url = f"https://demo-api.tradovate.com/v1/order/list"
    headers = {"Authorization": f"Bearer {client.access_token}"}
    start = asyncio.get_event_loop().time()
    while True:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.get(url, headers=headers)
            resp.raise_for_status()
            orders = resp.json()
            open_orders = [o for o in orders if o.get("symbol") == symbol and o.get("status") in ("Working", "Accepted")]
            if not open_orders:
                return
        if asyncio.get_event_loop().time() - start > timeout:
            logging.warning(f"Timeout waiting for all open orders to clear for {symbol}.")
            return
        await asyncio.sleep(0.5)

def parse_alert_to_tradovate_json(alert_text: str, account_id: int, latest_price: float = None) -> dict:
    logging.info(f"Raw alert text: {alert_text}")
    try:
        parsed_data = {}
        if alert_text.startswith("="):
            try:
                json_part, remaining_text = alert_text[1:].split("\n", 1)
                json_data = json.loads(json_part)
                parsed_data.update(json_data)
                alert_text = remaining_text
            except (json.JSONDecodeError, ValueError) as e:
                raise ValueError(f"Error parsing JSON-like structure: {e}")

        for line in alert_text.split("\n"):
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                parsed_data[key] = value
                logging.info(f"Parsed {key} = {value}")
            elif line.strip().upper() in ["BUY", "SELL"]:
                parsed_data["action"] = line.strip().capitalize()
                logging.info(f"Parsed action = {parsed_data['action']}")

        logging.info(f"Complete parsed alert data: {parsed_data}")

        required_fields = ["symbol", "action"]
        for field in required_fields:
            if field not in parsed_data or not parsed_data[field]:
                raise ValueError(f"Missing or invalid field: {field}")

        for target in ["T1", "STOP", "PRICE"]:
            if target in parsed_data:
                parsed_data[target] = float(parsed_data[target])
                logging.info(f"Converted {target} to float: {parsed_data[target]}")

        return parsed_data

    except Exception as e:
        logging.error(f"Error parsing alert: {e}")
        raise ValueError(f"Error parsing alert: {e}")

def hash_alert(data: dict) -> str:
    alert_string = json.dumps(data, sort_keys=True)
    return hashlib.sha256(alert_string.encode()).hexdigest()

async def monitor_all_orders(order_tracking, symbol, stop_order_data=None):
    """
    Monitor all orders and manage their relationships:
    - If ENTRY is filled, place the STOP order and keep TP active
    - If TP is filled, cancel STOP
    - If STOP is filled, cancel TP
    """
    logging.info(f"Starting comprehensive order monitoring for {symbol}")
    entry_filled = False
    
    while True:
        try:
            headers = {"Authorization": f"Bearer {client.access_token}"}
            active_orders = {}
            
            # Check status of all tracked orders
            for label, order_id in order_tracking.items():
                if order_id is None:
                    continue
                    
                url = f"https://demo-api.tradovate.com/v1/order/{order_id}"
                async with httpx.AsyncClient() as http_client:
                    response = await http_client.get(url, headers=headers)
                    response.raise_for_status()
                    order_status = response.json()
                    
                status = order_status.get("status")
                logging.info(f"Order {label} ({order_id}) status: {status}")
                
                if status == "Filled":
                    if label == "ENTRY" and not entry_filled:
                        logging.info(f"ENTRY order filled! Now placing STOP order for protection.")
                        entry_filled = True
                        
                        # Now place the STOP order since we're in position
                        if stop_order_data:
                            try:
                                # Fix the quantity field name in stop_order_data
                                stop_result = await client.place_order(
                                    symbol=symbol,
                                    action=stop_order_data["action"],
                                    quantity=stop_order_data["orderQty"],
                                    order_data=stop_order_data
                                )
                                order_tracking["STOP"] = stop_result.get("id")
                                logging.info(f"STOP order placed successfully: {stop_result}")
                            except Exception as e:
                                logging.error(f"Error placing STOP order after ENTRY fill: {e}")
                                # Try alternative approach if first attempt fails
                                try:
                                    headers = {"Authorization": f"Bearer {client.access_token}"}
                                    async with httpx.AsyncClient() as http_client:
                                        response = await http_client.post(
                                            f"https://demo-api.tradovate.com/v1/order/placeorder",
                                            headers=headers,
                                            json=stop_order_data
                                        )
                                        response.raise_for_status()
                                        stop_result = response.json()
                                        order_tracking["STOP"] = stop_result.get("id")
                                        logging.info(f"STOP order placed successfully (fallback): {stop_result}")
                                except Exception as e2:
                                    logging.error(f"Failed to place STOP order with fallback method: {e2}")
                        
                    elif label == "TP1" and entry_filled:
                        logging.info(f"TP1 order filled! Cancelling STOP order.")
                        if order_tracking.get("STOP"):
                            cancel_url = f"https://demo-api.tradovate.com/v1/order/cancel/{order_tracking['STOP']}"
                            async with httpx.AsyncClient() as http_client:
                                await http_client.post(cancel_url, headers=headers)
                        return  # Exit monitoring
                        
                    elif label == "STOP" and entry_filled:
                        logging.info(f"STOP order filled! Cancelling TP orders.")
                        if order_tracking.get("TP1"):
                            cancel_url = f"https://demo-api.tradovate.com/v1/order/cancel/{order_tracking['TP1']}"
                            async with httpx.AsyncClient() as http_client:
                                await http_client.post(cancel_url, headers=headers)
                        return  # Exit monitoring
                        
                elif status in ["Working", "Accepted"]:
                    active_orders[label] = order_id
                elif status in ["Cancelled", "Rejected"]:
                    logging.info(f"Order {label} was {status}")
                    
            # If no active orders remain, stop monitoring
            if not active_orders:
                logging.info("No active orders remaining. Stopping monitoring.")
                return
                
            await asyncio.sleep(1)
            
        except Exception as e:
            logging.error(f"Error in comprehensive order monitoring: {e}")
            await asyncio.sleep(5)

# Ensure deduplication logic is robust
@app.post("/webhook")
async def webhook(req: Request):
    global recent_alert_hashes
    logging.info("Webhook endpoint hit.")
    try:
        content_type = req.headers.get("content-type")
        raw_body = await req.body()

        latest_price = None

        if content_type == "application/json":
            data = await req.json()
        elif content_type.startswith("text/plain"):
            text_data = raw_body.decode("utf-8")
            if "symbol=" in text_data:
                latest_price = await get_latest_price(text_data.split("symbol=")[1].split(",")[0])
            data = parse_alert_to_tradovate_json(text_data, client.account_id, latest_price)
        else:
            raise HTTPException(status_code=400, detail="Unsupported content type")

        if WEBHOOK_SECRET is None:
            raise HTTPException(status_code=500, detail="Missing WEBHOOK_SECRET")

        # Deduplication logic
        current_hash = hash_alert(data)
        if current_hash in recent_alert_hashes:
            logging.warning("Duplicate alert received. Skipping execution.")
            return {"status": "duplicate", "detail": "Duplicate alert skipped."}
        recent_alert_hashes.add(current_hash)
        if len(recent_alert_hashes) > MAX_HASHES:
            recent_alert_hashes = set(list(recent_alert_hashes)[-MAX_HASHES:])

        action = data["action"].capitalize()
        symbol = data["symbol"]
        if symbol == "CME_MINI:NQ1!" or symbol == "NQ1!":
            symbol = "NQM5"

        # Check if this is an opposite direction alert before flattening
        current_direction = action.lower()
        
        # Get current position to check for opposite direction
        pos_url = f"https://demo-api.tradovate.com/v1/position/list"
        headers = {"Authorization": f"Bearer {client.access_token}"}
        async with httpx.AsyncClient() as http_client:
            pos_resp = await http_client.get(pos_url, headers=headers)
            pos_resp.raise_for_status()
            positions = pos_resp.json()
            
        existing_position = None
        for pos in positions:
            if pos.get("symbol") == symbol and abs(pos.get("netPos", 0)) > 0:
                existing_position = pos
                break
        
        # Determine if this is an opposite direction alert
        is_opposite_direction = False
        if existing_position:
            current_pos_qty = existing_position.get("netPos", 0)
            if (current_pos_qty > 0 and current_direction == "sell") or (current_pos_qty < 0 and current_direction == "buy"):
                is_opposite_direction = True
                logging.info(f"Opposite direction alert detected! Current position: {current_pos_qty}, new direction: {current_direction}")

        # Flatten all orders and positions at the beginning of each payload
        logging.info(f"Flattening all orders and positions for symbol: {symbol}")
        await cancel_all_orders(symbol)
        await flatten_position(symbol)
        await wait_until_no_open_orders(symbol, timeout=10)
        logging.info("All orders and positions flattened successfully.")

        # Check for open position (should be flat)
        async with httpx.AsyncClient() as http_client:
            pos_resp = await http_client.get(pos_url, headers=headers)
            pos_resp.raise_for_status()
            positions = pos_resp.json()
            for pos in positions:
                if pos.get("symbol") == symbol and abs(pos.get("netPos", 0)) > 0:
                    logging.warning(f"Position for {symbol} is not flat after flatten. Skipping order placement.")
                    return {"status": "skipped", "detail": "Position not flat after flatten."}
        
        # Check for open orders (should be none)
        order_url = f"https://demo-api.tradovate.com/v1/order/list"
        async with httpx.AsyncClient() as http_client:
            order_resp = await http_client.get(order_url, headers=headers)
            order_resp.raise_for_status()
            orders = order_resp.json()
            open_orders = [o for o in orders if o.get("symbol") == symbol and o.get("status") in ("Working", "Accepted")]
            if open_orders:
                logging.warning(f"Open orders for {symbol} still exist after cancel. Skipping order placement.")
                return {"status": "skipped", "detail": "Open orders still exist after cancel."}
        
        # Initialize the order plan
        order_plan = []
        stop_order_data = None
        logging.info("Creating order plan based on alert data")

        # Add limit orders for T1 (Take Profit)
        if "T1" in data:
            order_plan.append({
                "label": "TP1",
                "action": "Sell" if action.lower() == "buy" else "Buy",
                "orderType": "Limit",
                "price": data["T1"],
                "qty": 1
            })
            logging.info(f"Added limit order for TP1: {data['T1']}")
        else:
            logging.warning("T1 not found in alert data - no take profit order will be placed")

        # Add stop order for entry
        if "PRICE" in data:
            order_plan.append({
                "label": "ENTRY",
                "action": action,
                "orderType": "Stop",
                "stopPrice": data["PRICE"],
                "qty": 1
            })
            logging.info(f"Added stop order for entry at price: {data['PRICE']}")
        else:
            logging.warning("PRICE not found in alert data - no entry order will be placed")

        # Prepare stop loss order data (to be placed after entry is filled)
        if "STOP" in data:
            stop_order_data = {
                "accountId": client.account_id,
                "symbol": symbol,
                "action": "Sell" if action.lower() == "buy" else "Buy",
                "orderQty": 1,
                "orderType": "Stop",
                "stopPrice": data["STOP"],
                "timeInForce": "GTC",
                "isAutomated": True
            }
            logging.info(f"Prepared stop loss order data for placement after entry fill: {data['STOP']}")
        else:
            logging.warning("STOP not found in alert data - no stop loss order will be prepared")

        logging.info(f"Order plan created with {len(order_plan)} orders (STOP order will be placed after ENTRY fill)")
        
        # Log opposite direction handling result
        if is_opposite_direction:
            logging.info("Successfully handled opposite direction alert by flattening existing position")
        
        # Initialize variables for tracking orders
        order_results = []
        order_tracking = {
            "ENTRY": None,
            "TP1": None, 
            "STOP": None  # Will be set after ENTRY is filled
        }

        # Execute the order plan
        logging.info("Executing order plan")
        for order in order_plan:
            order_payload = {
                "accountId": client.account_id,
                "symbol": symbol,
                "action": order["action"],
                "orderQty": order["qty"],
                "orderType": order["orderType"],
                "price": order.get("price"),
                "stopPrice": order.get("stopPrice"),
                "timeInForce": "GTC",
                "isAutomated": True
            }

            logging.info(f"Placing order: {order_payload}")
            try:
                result = await client.place_order(
                    symbol=symbol,
                    action=order["action"],
                    quantity=order["qty"],
                    order_data=order_payload
                )
                logging.info(f"Order placed successfully: {result}")

                # Track the order ID
                order_id = result.get("id")
                order_tracking[order["label"]] = order_id
                
                order_results.append({order["label"]: result})
                
            except Exception as e:
                logging.error(f"Error placing order {order['label']}: {e}")
        
        # Start comprehensive monitoring of all orders
        asyncio.create_task(monitor_all_orders(order_tracking, symbol, stop_order_data))

        logging.info("Order plan execution completed")
        return {"status": "success", "order_responses": order_results}
    except Exception as e:
        logging.error(f"Unexpected error in webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
