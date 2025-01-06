from fastapi import FastAPI
from routers.auth import router as auth_router, create_admin_user
from routers.vendor import router as vendor_router
from routers.products import router as products_router
from routers.orderdetails import router as orderdetails_router  
from routers.orders import router as orders_router
import uvicorn
# Initialize the FastAPI application
app = FastAPI()

# Include the authentication router
app.include_router(auth_router, prefix="/auth", tags=["Authentication"])

# Include the vendor router
app.include_router(vendor_router, prefix="/vendors", tags=["Vendor Management"])

# Include the product router
app.include_router(products_router, prefix="/products", tags=["Product Management"])

# Include the order details router
app.include_router(orderdetails_router, prefix='/order-details', tags=["Order Details"])

app.include_router(orders_router, prefix='/orders', tags=["Orders"])

# Ensure the default admin user exists on application startup
@app.on_event("startup")
async def ensure_admin_user():
    """
    Startup event to create the default admin user if it doesn't exist.
    """
    await create_admin_user()

# Run the FastAPI application
if __name__ == "__main__":
    uvicorn.run("main:app", port=8001, host='127.0.0.1', reload=True)
