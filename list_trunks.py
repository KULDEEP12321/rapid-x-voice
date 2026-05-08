"""List all inbound and outbound SIP trunks on the configured LiveKit project."""

import asyncio
import os
import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv
from livekit import api
from livekit.protocol.sip import (
    ListSIPInboundTrunkRequest,
    ListSIPOutboundTrunkRequest,
)

import config

load_dotenv(".env")


async def main():
    if not (config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET):
        print("Error: missing LiveKit credentials in .env")
        return

    lkapi = api.LiveKitAPI(
        url=config.LIVEKIT_URL,
        api_key=config.LIVEKIT_API_KEY,
        api_secret=config.LIVEKIT_API_SECRET,
    )

    try:
        out = (await lkapi.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())).items
        print(f"Outbound trunks ({len(out)}):")
        for t in out:
            print(f"  {t.sip_trunk_id}  {t.name}  numbers={list(t.numbers)}")

        inb = (await lkapi.sip.list_inbound_trunk(ListSIPInboundTrunkRequest())).items
        print(f"\nInbound trunks ({len(inb)}):")
        for t in inb:
            print(f"  {t.sip_trunk_id}  {t.name}  numbers={list(t.numbers)}")
    except Exception as e:
        print(f"Error listing trunks: {e}")
    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    asyncio.run(main())
