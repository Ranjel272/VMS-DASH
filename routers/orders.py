from fastapi import FastAPI, HTTPException, Query, APIRouter
from pydantic import BaseModel
from datetime import datetime, timedelta
import logging
import httpx
import database
import asyncio
import aiohttp
from aiohttp import ClientSession


class OrderStatusUpdate(BaseModel):
    orderStatus: str

router = APIRouter()

# Helper function to send order to IMS
async def send_to_ims_api(ims_api_url: str, payload: dict):
    try:
        logging.info(f'Sending data to IMS API: {ims_api_url}')
        logging.debug(f'Payload: {payload}')

        async with httpx.AsyncClient() as client:
            response = await client.post(ims_api_url, json=payload)
            response.raise_for_status()
            logging.info(f'Response received from IMS API: {response.json()}')
            return response.json()

    except httpx.HTTPStatusError as http_err:
        logging.error(f"HTTP error occurred: {http_err.response.status_code} - {http_err.response.text}")
        raise HTTPException(status_code=500, detail=f"IMS API error: {http_err.response.text}")
    except Exception as e:
        logging.error(f"Error sending data to IMS API: {e}")
        raise HTTPException(status_code=500, detail=f'Error sending data to IMS API: {e}')


# Receive order from VMS
@router.post('/vms/orders')
async def receive_order(order: dict):
    conn = None
    try:
        # Extract order details
        customer_id = order.get('customerID')
        order_date = order.get('orderDate', datetime.utcnow())
        order_status = 'Pending'
        products = order.get('products', [])

        # Validate the incoming data
        if not customer_id or not products:
            raise HTTPException(status_code=400, detail="Invalid order data.")

        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Save order in VMS
        await cursor.execute(
            '''INSERT INTO purchaseOrders (orderDate, orderStatus, statusDate, customerID)
               OUTPUT inserted.orderID VALUES (?, ?, ?, ?)''',
            (order_date, order_status, datetime.utcnow(), customer_id)
        )
        order_id = await cursor.fetchone()
        if not order_id:
            raise HTTPException(status_code=500, detail='Failed to create purchase order.')

        # Insert purchase order details
        for product in products:
            product_id = product.get('productID')
            quantity = product.get('quantity')
            expected_date = product.get('expectedDate', (datetime.utcnow() + timedelta(days=7)))

            if not product_id or not quantity:
                raise HTTPException(status_code=400, detail="Invalid product details")

            await cursor.execute(
                '''INSERT INTO purchaseOrderDetails 
                   (orderQuantity, expectedDate, productID, orderID)
                   VALUES (?, ?, ?, ?)''',
                (quantity, expected_date, product_id, order_id[0])
            )
        await conn.commit()
        return {"message": "Order received successfully.", "orderID": order_id[0]}
    except Exception as e:
        logging.error(f"Error receiving order: {e}")
        raise HTTPException(status_code=500, detail=f"Error receiving order: {e}")
    finally:
        if conn:
            await conn.close()

@router.put("/vms/orders/{orderID}/confirm")
async def confirm_order(orderID: int, order_status_update: OrderStatusUpdate):
    conn = None
    try:
        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Check if the order exists in VMS
        await cursor.execute('''SELECT orderStatus FROM dbo.purchaseOrders WHERE orderID = ?''', (orderID,))
        order = await cursor.fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Only allow "Pending" orders to be confirmed or rejected
        if order[0] != "Pending":
            raise HTTPException(status_code=400, detail="Only pending orders can be confirmed or rejected")

        # Validate the status provided
        status = order_status_update.orderStatus
        if status not in ["Confirmed", "Rejected"]:
            raise HTTPException(status_code=400, detail="Invalid order status. Must be 'Confirmed' or 'Rejected'.")

        # Fetch order details for the products
        await cursor.execute('''SELECT productID, orderQuantity FROM purchaseOrderDetails WHERE orderID = ?''', (orderID,))
        products = await cursor.fetchall()

        # Check the availability of product variants
        for product in products:
            product_id = product[0]
            order_quantity = product[1]

            # Fetch available variants for the product
            variant_query = f'''SELECT TOP ({order_quantity}) pv.barcode, pv.productCode, p.productName, 
                                       p.category, p.color, p.size 
                                FROM productVariants pv
                                JOIN products p ON pv.productID = p.productID
                                WHERE pv.productID = ? AND pv.isAvailable = 1
                                ORDER BY pv.variantID ASC'''
            await cursor.execute(variant_query, (product_id,))
            variants = await cursor.fetchall()

            # Check if there are enough available variants
            if len(variants) < order_quantity:
                raise HTTPException(
                    status_code=400,
                    detail=f'Not enough available variants for productID {product_id}. Required: {order_quantity}, Available: {len(variants)}'
                )

        # Prepare the payload for IMS if the status is "Confirmed"
        ims_api_url = 'http://127.0.0.1:8000/ims/receive_orders/ims/orders/confirm'
        ims_payload = {"orderID": orderID, "orderStatus": status}

        # Send the confirmation or rejection to IMS and wait for a response
        ims_response = await send_to_ims_api(ims_api_url, ims_payload)

        # Update the status in VMS immediately after receiving response from IMS
        await cursor.execute(
            '''UPDATE purchaseOrders SET orderStatus = ?, statusDate = ? WHERE orderID = ?''',
            (status, datetime.utcnow(), orderID)
        )
        await conn.commit()

        return {"message": f"Order {orderID} has been {status} in VMS.", 'imsResponse': ims_response}

    except Exception as e:
        logging.error(f"Error confirming order: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing order: {e}")
    finally:
        if conn:
            await conn.close()

@router.get('/vms/orders/confirmed')
async def get_confirmed_orders():
    conn = None
    try:
        # Get a database connection
        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Fetch all orders with status "Confirmed"
        await cursor.execute('''
            SELECT orderID, orderDate, statusDate, customerID
            FROM purchaseOrders
            WHERE orderStatus = 'Confirmed'
        ''')
        orders = await cursor.fetchall()

        # If no orders found, return an empty list
        if not orders:
            return {"message": "No confirmed orders found", "orders": []}

        # Format the results into a list of dictionaries
        result = [
            {
                "orderID": order[0],
                "orderDate": order[1],
                "statusDate": order[2],
                "customerID": order[3],
            }
            for order in orders
        ]

        return {"message": "Confirmed orders retrieved successfully", "orders": result}

    except Exception as e:
        logging.error(f"Error fetching confirmed orders: {e}")
        raise HTTPException(status_code=500, detail="Error fetching confirmed orders")
    finally:
        if conn:
            await conn.close()

@router.put('/vms/orders/{orderID}/toship')
async def mark_to_ship(orderID: int):
    conn = None
    try:
        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Validate order status in VMS
        await cursor.execute(
            '''SELECT orderStatus 
            FROM purchaseOrders 
            WHERE orderID = ?''',
            (orderID,)
        )
        order = await cursor.fetchone()
        if not order or order[0] != 'Confirmed':
            raise HTTPException(status_code=400, detail="Order is not in 'Confirmed' status.")
        
        # Update to "To Ship" in VMS
        await cursor.execute(
            '''UPDATE purchaseOrders 
            SET orderStatus = 'To Ship',
            statusDate = ? 
            WHERE orderID = ?''',
            (datetime.utcnow(), orderID)
        )
        await conn.commit()

        # After updating VMS, also update IMS with the "To Ship" status
        ims_url = 'http://127.0.0.1:8000/ims/receive_orders/ims/orders/ToShip'  

        ims_payload = {
            "orderID": orderID,
            "orderStatus": "To Ship"
        }

        # Make the API call to IMS to update the order status
        async with httpx.AsyncClient() as client:
            ims_response = await client.post(ims_url, json=ims_payload)
            ims_response.raise_for_status()  

        # Log the IMS response for debugging
        logging.info(f"IMS Response: {ims_response.status_code} - {ims_response.text}")

        return {'message': f"Order {orderID} marked as 'To Ship' in VMS and updated in IMS."}

    except HTTPException as http_err:
        logging.error(f"HTTP Error: {http_err}")
        raise http_err
    except Exception as e:
        logging.error(f"Error updating order to 'To Ship': {e}")
        raise HTTPException(status_code=500, detail=f"Error processing the update: {e}")
    finally:
        if conn:
            await conn.close()

from datetime import datetime

# Define the send_to_ims_api_with_retries function
async def send_to_ims_api_with_retries(url, payload, retries=3, delay=2):
    for attempt in range(retries):
        try:
            async with ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        return await response.json()
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")
        await asyncio.sleep(delay)
    raise HTTPException(status_code=500, detail="Failed to send data to IMS after multiple attempts.")

@router.get('/vms/orders/toship')
async def get_to_ship_orders():
    conn = None
    try:
        # Get a database connection
        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Fetch all orders with status "To Ship"
        await cursor.execute('''
            SELECT orderID, orderDate, statusDate, customerID
            FROM purchaseOrders
            WHERE orderStatus = 'To Ship'
        ''')
        orders = await cursor.fetchall()

        # If no orders found, return an empty list
        if not orders:
            return {"message": "No 'To Ship' orders found", "orders": []}

        # Format the results into a list of dictionaries
        result = [
            {
                "orderID": order[0],
                "orderDate": order[1],
                "statusDate": order[2],
                "customerID": order[3],
            }
            for order in orders
        ]

        return {"message": "'To Ship' orders retrieved successfully", "orders": result}

    except Exception as e:
        logging.error(f"Error fetching 'To Ship' orders: {e}")
        raise HTTPException(status_code=500, detail="Error fetching 'To Ship' orders")
    finally:
        if conn:
            await conn.close()

@router.put('/vms/orders/{orderID}/ship')
async def ship_order(orderID: int):
    conn = None
    try:
        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Validate order status
        await cursor.execute(
            '''SELECT orderStatus
               FROM purchaseOrders
               WHERE orderID = ?''',
            (orderID,)
        )
        order = await cursor.fetchone()
        if not order or order[0] != 'To Ship':
            raise HTTPException(status_code=400, detail="Order is not in 'To Ship' status.")

        # Fetch products and order quantities
        await cursor.execute(
            '''SELECT pod.productID, pod.orderQuantity
               FROM purchaseOrderDetails pod
               WHERE pod.orderID = ?''',
            (orderID,)
        )
        products = await cursor.fetchall()
        if not products:
            raise HTTPException(status_code=404, detail="No products found for this order.")

        # Prepare the list of product variants to send to IMS
        variant_data = []
        for product in products:
            product_id, order_quantity = product

            order_quantity = int(order_quantity)

            # Fetch only the required number of product variants (orderQuantity)
            await cursor.execute(
                '''SELECT TOP (?) pv.barcode, pv.productCode, p.productName, p.category, p.color, p.size
                   FROM productVariants pv
                   JOIN products p ON pv.productID = p.productID
                   WHERE pv.productID = ? AND pv.isAvailable = 1
                   ORDER BY pv.variantID ASC''',  
                (order_quantity, product_id)
            )
            variants = await cursor.fetchall()
            logging.info(f"Available variants for productID {product_id}: {len(variants)}")
            
            if len(variants) < order_quantity:
                raise HTTPException(
                    status_code=400,
                    detail=f"Not enough available variants for productID {product_id}. "
                           f"Required: {order_quantity}, Available: {len(variants)}"
                )

            # Add the fetched variants to the variant_data list
            variant_data.extend([{
                "barcode": v[0],
                "productCode": v[1],
                "productName": v[2],
                "category": v[3],
                "color": v[4],
                "size": v[5]
            } for v in variants[:order_quantity]])  
            
        # Send the prepared variants to IMS
        ims_api_url = 'http://127.0.0.1:8000/ims/receive_orders/ims/variants/receive'
        payload = {
            'orderID': orderID,
            'orderStatus': 'Shipped',  
            'variants': variant_data
        }
        logging.info(f"Sending payload to IMS: {payload}")
        ims_response = await send_to_ims_api_with_retries(ims_api_url, payload)

        if ims_response.get('status') != 'success':
            raise HTTPException(status_code=500, detail="Failed to send order data to IMS.")

        # Update order status to 'Shipped'
        status_date = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        await cursor.execute(
            '''UPDATE purchaseOrders
               SET orderStatus = 'Shipped', statusDate = ?
               WHERE orderID = ?''',
            (status_date, orderID)
        )

        # Mark selected variants as unavailable
        await cursor.executemany(
            '''UPDATE productVariants
               SET isAvailable = 0
               WHERE barcode = ?''',
            [(variant['barcode'],) for variant in variant_data]
        )

        # Commit the changes
        await conn.commit()

        return {
            'message': f"Order {orderID} marked as 'Shipped' and variants sent to IMS successfully.",
            'imsResponse': ims_response
        }

    except Exception as e:
        logging.error(f"Error shipping order: {e}")
        raise HTTPException(status_code=500, detail=f"Error shipping order: {e}")
    finally:
        if conn:
            await conn.close()


async def send_to_ims_api_with_retries(url, payload, retries=3, delay=2):
    for attempt in range(retries):
        try:
            async with ClientSession() as session:
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        return await response.json()
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {e}")
        await asyncio.sleep(delay)
    raise HTTPException(status_code=500, detail="Failed to send data to IMS after multiple attempts.")

@router.get('/vms/orders/shipped')
async def get_shipped_orders():
    conn = None
    try:
        # Get a database connection
        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Fetch all orders with status "Shipped"
        await cursor.execute('''
            SELECT orderID, orderDate, statusDate, customerID
            FROM purchaseOrders
            WHERE orderStatus = 'Shipped'
        ''')
        orders = await cursor.fetchall()

        # If no orders found, return an empty list
        if not orders:
            return {"message": "No shipped orders found", "orders": []}

        # Format the results into a list of dictionaries
        result = [
            {
                "orderID": order[0],
                "orderDate": order[1],
                "statusDate": order[2],
                "customerID": order[3],
            }
            for order in orders
        ]

        return {"message": "Shipped orders retrieved successfully", "orders": result}

    except Exception as e:
        logging.error(f"Error fetching shipped orders: {e}")
        raise HTTPException(status_code=500, detail="Error fetching shipped orders")
    finally:
        if conn:
            await conn.close()