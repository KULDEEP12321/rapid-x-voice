"""CLI: dispatch the outbound voice agent into a fresh room and dial a number."""

import argparse
import asyncio
import json
import logging
import os
import random

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv
from livekit import api

import config

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)


async def main():
    parser = argparse.ArgumentParser(description="Dispatch an outbound LiveKit voice call.")
    parser.add_argument("--to", required=True, help="Phone number in E.164 format, e.g. +91...")
    parser.add_argument("--prompt", default="", help="Extra campaign context for this call.")
    parser.add_argument("--lead-context", default="", help="Prefetched caller/CRM context for the agent.")
    parser.add_argument("--voice", default=None, help=f"Gemini voice (default {config.GEMINI_VOICE}).")
    args = parser.parse_args()

    phone = args.to.strip()
    if not phone.startswith("+") or len(phone) < 8:
        print("Error: phone must start with '+' and include country code.")
        return

    if not (config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET):
        print("Error: LiveKit credentials missing in .env")
        return

    lk = api.LiveKitAPI(
        url=config.LIVEKIT_URL,
        api_key=config.LIVEKIT_API_KEY,
        api_secret=config.LIVEKIT_API_SECRET,
    )

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    print(f"Dispatching call to {phone} in room {room_name}...")

    try:
        dispatch = await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name="outbound-caller",
                room=room_name,
                metadata=json.dumps({
                    "phone_number": phone,
                    "user_prompt": args.prompt,
                    "lead_context": args.lead_context or args.prompt,
                    "voice_id": args.voice,
                }),
            )
        )
        print(f"Dispatched. id={dispatch.id} room={room_name}")
    except Exception as e:
        print(f"Dispatch failed: {e}")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
