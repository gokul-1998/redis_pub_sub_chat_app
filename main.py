import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Dict, Set
import uuid

import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Redis Pub/Sub Notification System")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis configuration
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6389")
redis_client = None
pubsub = None

# Connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.user_connections: Dict[str, str] = {}  # user_id -> connection_id

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        connection_id = str(uuid.uuid4())
        self.active_connections[connection_id] = websocket
        self.user_connections[user_id] = connection_id
        logger.info(f"User {user_id} connected with connection {connection_id}")
        return connection_id

    def disconnect(self, connection_id: str, user_id: str):
        if connection_id in self.active_connections:
            del self.active_connections[connection_id]
        if user_id in self.user_connections:
            del self.user_connections[user_id]
        logger.info(f"User {user_id} disconnected")

    async def send_personal_message(self, message: str, user_id: str):
        connection_id = self.user_connections.get(user_id)
        if connection_id and connection_id in self.active_connections:
            try:
                await self.active_connections[connection_id].send_text(message)
                return True
            except Exception as e:
                logger.error(f"Error sending message to user {user_id}: {e}")
                return False
        return False

    async def broadcast(self, message: str):
        disconnected = []
        for connection_id, websocket in self.active_connections.items():
            try:
                await websocket.send_text(message)
            except Exception as e:
                logger.error(f"Error broadcasting to connection {connection_id}: {e}")
                disconnected.append(connection_id)
        
        # Clean up disconnected connections
        for connection_id in disconnected:
            if connection_id in self.active_connections:
                del self.active_connections[connection_id]

    def is_user_connected(self, user_id: str) -> bool:
        return user_id in self.user_connections

manager = ConnectionManager()

# Pydantic models
class Message(BaseModel):
    user_id: str
    content: str
    message_type: str = "notification"

class BroadcastMessage(BaseModel):
    content: str
    message_type: str = "broadcast"

# Redis operations
async def get_redis():
    global redis_client
    if not redis_client:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return redis_client

async def store_offline_message(user_id: str, message: dict):
    """Store message for offline user"""
    r = await get_redis()
    key = f"offline_messages:{user_id}"
    await r.lpush(key, json.dumps(message))
    # Set expiration for 7 days
    await r.expire(key, 604800)
    logger.info(f"Stored offline message for user {user_id}")

async def get_offline_messages(user_id: str):
    """Retrieve and clear offline messages for user"""
    r = await get_redis()
    key = f"offline_messages:{user_id}"
    messages = await r.lrange(key, 0, -1)
    if messages:
        await r.delete(key)
        return [json.loads(msg) for msg in reversed(messages)]
    return []

async def publish_message(channel: str, message: dict):
    """Publish message to Redis channel"""
    r = await get_redis()
    await r.publish(channel, json.dumps(message))

# Redis subscriber
async def redis_subscriber():
    """Subscribe to Redis channels and handle incoming messages"""
    global pubsub
    r = await get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("notifications", "broadcasts")
    
    logger.info("Redis subscriber started")
    
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    channel = message["channel"]
                    
                    if channel == "broadcasts":
                        # Broadcast to all connected users
                        await manager.broadcast(json.dumps(data))
                        logger.info("Broadcasted message to all users")
                        
                        # Also store broadcast for all offline users
                        # Get all user IDs that have offline messages
                        keys = await r.keys("offline_messages:*")
                        offline_user_ids = [key.split(":")[1] for key in keys]
                        
                        # Get all user IDs that have ever connected (from a tracking set)
                        all_users = await r.smembers("all_users")
                        
                        # Combine both sets of user IDs
                        user_ids = set(offline_user_ids) | set(all_users)
                        
                        # Store the broadcast for users who are not currently connected
                        for user_id in user_ids:
                            if not manager.is_user_connected(user_id):
                                await store_offline_message(user_id, data)
                                logger.info(f"Stored broadcast message for offline user {user_id}")
                    
                    elif channel == "notifications":
                        # Send to specific user
                        user_id = data.get("user_id")
                        if user_id:
                            sent = await manager.send_personal_message(
                                json.dumps(data), user_id
                            )
                            if not sent:
                                # User not connected, store for later
                                await store_offline_message(user_id, data)
                                
                except json.JSONDecodeError as e:
                    logger.error(f"Error decoding message: {e}")
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
    except asyncio.CancelledError:
        logger.info("Redis subscriber cancelled")
    except Exception as e:
        logger.error(f"Redis subscriber error: {e}")

# Startup and shutdown events
@app.on_event("startup")
async def startup_event():
    # Start Redis subscriber in background
    asyncio.create_task(redis_subscriber())
    logger.info("Application started")

@app.on_event("shutdown")
async def shutdown_event():
    global redis_client, pubsub
    if pubsub:
        await pubsub.close()
    if redis_client:
        await redis_client.close()
    logger.info("Application shutdown")

# WebSocket endpoint
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    connection_id = await manager.connect(websocket, user_id)
    
    try:
        # Add user to the set of all users who have ever connected
        r = await get_redis()
        await r.sadd("all_users", user_id)
        
        # Send any offline messages to the newly connected user
        offline_messages = await get_offline_messages(user_id)
        for msg in offline_messages:
            await websocket.send_text(json.dumps(msg))
            logger.info(f"Sent offline message to user {user_id}")
        
        # Keep connection alive and handle incoming messages
        while True:
            data = await websocket.receive_text()
            # Echo back or handle client messages if needed
            await websocket.send_text(f"Echo: {data}")
            
    except WebSocketDisconnect:
        manager.disconnect(connection_id, user_id)
    except Exception as e:
        logger.error(f"WebSocket error for user {user_id}: {e}")
        manager.disconnect(connection_id, user_id)

# REST API endpoints
@app.post("/send-notification")
async def send_notification(message: Message):
    """Send notification to specific user"""
    try:
        message_data = {
            "user_id": message.user_id,
            "content": message.content,
            "message_type": message.message_type,
            "timestamp": datetime.now().isoformat()
        }
        
        await publish_message("notifications", message_data)
        
        return {
            "status": "success", 
            "message": "Notification sent",
            "user_connected": manager.is_user_connected(message.user_id)
        }
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        raise HTTPException(status_code=500, detail="Failed to send notification")

@app.post("/broadcast")
async def broadcast_message(message: BroadcastMessage):
    """Broadcast message to all connected users"""
    try:
        message_data = {
            "content": message.content,
            "message_type": message.message_type,
            "timestamp": datetime.now().isoformat()
        }
        
        await publish_message("broadcasts", message_data)
        
        return {
            "status": "success", 
            "message": "Broadcast sent",
            "connected_users": len(manager.active_connections)
        }
    except Exception as e:
        logger.error(f"Error broadcasting message: {e}")
        raise HTTPException(status_code=500, detail="Failed to broadcast message")

@app.get("/users/{user_id}/status")
async def get_user_status(user_id: str):
    """Check if user is connected and count offline messages"""
    r = await get_redis()
    offline_count = await r.llen(f"offline_messages:{user_id}")
    
    return {
        "user_id": user_id,
        "connected": manager.is_user_connected(user_id),
        "offline_messages": offline_count
    }

@app.get("/stats")
async def get_stats():
    """Get system statistics"""
    return {
        "connected_users": len(manager.active_connections),
        "total_connections": len(manager.user_connections)
    }

@app.get("/")
async def root():
    return {"message": "Redis Pub/Sub Notification System"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)