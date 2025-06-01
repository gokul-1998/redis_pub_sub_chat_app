#!/usr/bin/env python3
"""
Test script to verify that broadcast messages are received after a user reconnects.
This script:
1. Connects a user to the WebSocket server
2. Disconnects the user
3. Sends a broadcast message
4. Reconnects the user
5. Verifies that the broadcast message is received
"""

import asyncio
import json
import websockets
import requests
import time
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
API_BASE_URL = "http://localhost:8001"
WS_BASE_URL = "ws://localhost:8001"
TEST_USER_ID = "test_user_reconnect"
BROADCAST_MESSAGE = "Test broadcast message while user is offline"

async def connect_websocket(user_id):
    """Connect to WebSocket and return the connection"""
    ws_url = f"{WS_BASE_URL}/ws/{user_id}"
    logger.info(f"Connecting to {ws_url}")
    
    try:
        ws = await websockets.connect(ws_url)
        logger.info(f"Connected to WebSocket as user {user_id}")
        return ws
    except Exception as e:
        logger.error(f"Failed to connect to WebSocket: {e}")
        raise

async def receive_messages(ws, timeout=5):
    """Receive and log messages for a specified time period"""
    messages = []
    try:
        # Set a timeout to receive messages
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                # Set a short timeout for each receive operation
                message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                try:
                    parsed_message = json.loads(message)
                    logger.info(f"Received message: {parsed_message}")
                    messages.append(parsed_message)
                except json.JSONDecodeError:
                    logger.info(f"Received non-JSON message: {message}")
                    messages.append(message)
            except asyncio.TimeoutError:
                # This is expected, just continue the loop
                pass
    except Exception as e:
        logger.error(f"Error receiving messages: {e}")
    
    return messages

def send_broadcast(message):
    """Send a broadcast message via the REST API"""
    url = f"{API_BASE_URL}/broadcast"
    payload = {
        "content": message,
        "message_type": "broadcast"
    }
    
    logger.info(f"Sending broadcast message: {message}")
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        logger.info("Broadcast sent successfully")
        return response.json()
    else:
        logger.error(f"Failed to send broadcast: {response.status_code} - {response.text}")
        return None

async def test_broadcast_after_reconnect():
    """Test that broadcast messages are received after reconnecting"""
    # Step 1: Connect to WebSocket
    ws = await connect_websocket(TEST_USER_ID)
    
    # Receive initial messages (if any)
    logger.info("Checking for initial messages...")
    initial_messages = await receive_messages(ws, timeout=2)
    
    # Step 2: Disconnect
    logger.info("Disconnecting from WebSocket...")
    await ws.close()
    logger.info("Disconnected")
    
    # Wait a moment to ensure the server registers the disconnection
    await asyncio.sleep(1)
    
    # Step 3: Send a broadcast message while disconnected
    broadcast_result = send_broadcast(BROADCAST_MESSAGE)
    if not broadcast_result:
        logger.error("Failed to send broadcast message, aborting test")
        return False
    
    # Wait a moment to ensure the message is processed
    await asyncio.sleep(1)
    
    # Step 4: Reconnect
    logger.info("Reconnecting to WebSocket...")
    ws = await connect_websocket(TEST_USER_ID)
    
    # Step 5: Check if we receive the broadcast message
    logger.info("Checking for broadcast message after reconnection...")
    reconnect_messages = await receive_messages(ws, timeout=3)
    
    # Close the connection
    await ws.close()
    
    # Verify the test result
    broadcast_received = False
    for message in reconnect_messages:
        if isinstance(message, dict) and message.get("content") == BROADCAST_MESSAGE:
            broadcast_received = True
            break
        elif isinstance(message, str):
            try:
                parsed = json.loads(message)
                if parsed.get("content") == BROADCAST_MESSAGE:
                    broadcast_received = True
                    break
            except json.JSONDecodeError:
                pass
    
    if broadcast_received:
        logger.info("✅ TEST PASSED: Broadcast message was received after reconnection")
        return True
    else:
        logger.error("❌ TEST FAILED: Broadcast message was not received after reconnection")
        logger.info(f"Messages received after reconnection: {reconnect_messages}")
        return False

async def main():
    """Run the test"""
    try:
        logger.info("Starting broadcast reconnection test")
        result = await test_broadcast_after_reconnect()
        logger.info(f"Test completed with result: {'PASS' if result else 'FAIL'}")
    except Exception as e:
        logger.error(f"Test failed with exception: {e}")

if __name__ == "__main__":
    asyncio.run(main())
