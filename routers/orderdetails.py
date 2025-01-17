from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from typing import List
import database  

# Create a response model for the order details
class OrderDetails(BaseModel):
    orderID: int
    productID: int
    productName: str
    quantity: int
    warehouseID: int
    vendorID: int
    vendorName: Optional[str] = "Not provided"
    customerFirstName: Optional[str] = "Not provided"
    customerLastName: Optional[str] = "Not provided"
    orderDate: Optional[datetime] = None
    expectedDate: Optional[datetime] = None
    userID: int

class PurchaseOrder(BaseModel):
    orderID: int
    orderDate: datetime
    expectedDate: Optional[datetime] = None  
    orderStatus: str
    statusDate: datetime

# Create a router for order details
router = APIRouter()

@router.post("/orders", response_model=OrderDetails)
async def display_order(payload: dict):
    try:
        # Log the incoming payload for debugging purposes
        print("Received Payload:", payload)

        # Validate required fields
        required_fields = ["orderID", "productName", "productDescription", "size", "color", "category", "quantity", "warehouseID", "vendorID", "userID"]
        missing_fields = [field for field in required_fields if field not in payload]
        if missing_fields:
            raise HTTPException(
                status_code=400, detail=f"Missing required fields: {', '.join(missing_fields)}"
            )

        # Establish database connection
        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Check if the product exists in the Products table
        product_query = """
        SELECT productID FROM Products 
        WHERE productName = ? AND productDescription = ? AND size = ? AND color = ? AND category = ?
        """
        await cursor.execute(
            product_query,
            (payload["productName"], payload["productDescription"], payload["size"], payload["color"], payload["category"])
        )
        product_result = await cursor.fetchone()
        if not product_result:
            raise HTTPException(status_code=404, detail="Product not found in the database.")

        product_id = product_result[0] 

        # Create OrderDetails instance
        order_details = OrderDetails(
            orderID=payload.get("orderID"),
            productID=product_id,
            productName=payload.get("productName"),
            quantity=payload.get("quantity"),
            warehouseID=payload.get("warehouseID"),
            vendorID=payload.get("vendorID"),
            userID=payload.get("userID"),
            vendorName=payload.get("vendorName", "Not provided"),
            orderDate=payload.get("orderDate"),
            expectedDate=payload.get("expectedDate"),
        )

        # Fetch vendorName based on vendorID
        await cursor.execute("SELECT TOP 1 vendorName FROM vendors WHERE vendorID = ? AND isActive = 1", (order_details.vendorID,))
        vendor_result = await cursor.fetchone()
        if vendor_result:
            order_details.vendorName = vendor_result[0]
        else:
            order_details.vendorName = "Vendor not found or inactive"

        # Convert orderDate and expectedDate to proper formats
        order_date = datetime.strptime(payload["orderDate"], '%Y-%m-%d') if payload.get("orderDate") else None
        expected_date = datetime.strptime(payload["expectedDate"], '%Y-%m-%d') if payload.get("expectedDate") else None
        status_date = datetime.now().strftime('%Y-%m-%d')

        # Ensure the customer exists in the Customers table
        await cursor.execute("SELECT customerID FROM Customers WHERE customerID = ?", (payload["userID"],))
        customer_record = await cursor.fetchone()
        if not customer_record:
            await cursor.execute("SET IDENTITY_INSERT Customers ON")

            await cursor.execute(
                """
                INSERT INTO Customers (customerID, customerName, customerWarehouseName, customerAddress)
                VALUES (?, ?, ?, ?)
                """,
                (
                    payload["userID"],
                    payload["userName"],
                    payload["warehouseName"],
                    payload["warehouseAddress"],
                ),
            )
            await cursor.execute("SET IDENTITY_INSERT Customers OFF")

            await conn.commit()
            customer_id = payload["userID"]
        else:
            customer_id = customer_record[0]

        # Insert into purchaseOrders table
        await cursor.execute("SET IDENTITY_INSERT purchaseOrders ON")
        await cursor.execute(
            """
            INSERT INTO purchaseOrders (orderID, vendorID, customerID, orderDate, orderStatus, statusDate)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                order_details.orderID,
                order_details.vendorID,
                customer_id,
                order_date.strftime('%Y-%m-%d') if order_date else None,
                "Pending",
                status_date,
            ),
        )
        await cursor.execute("SET IDENTITY_INSERT purchaseOrders OFF")
        await conn.commit()

        # Insert into purchaseOrderDetails table
        await cursor.execute(
            """
            INSERT INTO purchaseOrderDetails (orderID, productID, orderQuantity, expectedDate)
            VALUES (?, ?, ?, ?)
            """,
            (
                order_details.orderID,
                product_id,
                payload["quantity"],
                expected_date.strftime('%Y-%m-%d') if expected_date else None,
            ),
        )
        await conn.commit()

        # Close cursor and connection
        await cursor.close()
        await conn.close()

        return order_details

    except Exception as e:
        print(f"Error occurred: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing the order: {str(e)}")
    
@router.get("/orders/pending", response_model=List[PurchaseOrder])
async def get_pending_orders():
    try:
        # Establish database connection
        conn = await database.get_db_connection()
        cursor = await conn.cursor()

        # Query to fetch all orders with the status 'Pending'
        query = """
        SELECT 
            orderID,
            orderDate,
            expectedDate,
            orderStatus,
            statusDate
        FROM 
            purchaseOrders
        WHERE 
            orderStatus = 'Pending'
        """
        await cursor.execute(query)
        orders = await cursor.fetchall()

        # Convert the results to a list of PurchaseOrder objects
        pending_orders = [
                PurchaseOrder(
        orderID=row[0],
        orderDate=row[1],
        expectedDate=row[2] if row[2] else None,  # Handle None values
        orderStatus=row[3],
        statusDate=row[4],
    )
    for row in orders
]

        # Close cursor and connection
        await cursor.close()
        await conn.close()

        return pending_orders

    except Exception as e:
        print(f"Error occurred: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching pending orders: {str(e)}")