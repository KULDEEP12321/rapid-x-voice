"""Create a LiveKit outbound SIP trunk from .env credentials."""

import asyncio
import os
import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv
from livekit import api
from livekit.protocol.sip import CreateSIPOutboundTrunkRequest, SIPOutboundTrunkInfo

import config

load_dotenv(".env")


async def main():
    if not (config.LIVEKIT_URL and config.LIVEKIT_API_KEY and config.LIVEKIT_API_SECRET):
        print("Error: missing LiveKit credentials in .env")
        return
    if not (config.SIP_DOMAIN and config.SIP_USERNAME and config.SIP_PASSWORD):
        print("Error: missing SIP credentials (SIP_DOMAIN / SIP_USERNAME / SIP_PASSWORD)")
        return

    lkapi = api.LiveKitAPI(
        url=config.LIVEKIT_URL,
        api_key=config.LIVEKIT_API_KEY,
        api_secret=config.LIVEKIT_API_SECRET,
    )

    try:
        print(f"Creating SIP trunk for {config.SIP_DOMAIN}...")
        trunk_info = SIPOutboundTrunkInfo(
            name="Vobiz Trunk",
            address=config.SIP_DOMAIN,
            auth_username=config.SIP_USERNAME,
            auth_password=config.SIP_PASSWORD,
            numbers=[config.SIP_OUTBOUND_NUMBER] if config.SIP_OUTBOUND_NUMBER else [],
        )
        trunk = await lkapi.sip.create_outbound_trunk(
            CreateSIPOutboundTrunkRequest(trunk=trunk_info)
        )
        print("\nSIP trunk created.")
        print(f"  Trunk ID: {trunk.sip_trunk_id}")
        print(f"  Name:     {trunk.name}")
        print(f"  Numbers:  {list(trunk.numbers)}")
        print("\nAdd this to .env:  SIP_TRUNK_ID=" + trunk.sip_trunk_id)
    except Exception as e:
        print(f"\nFailed to create trunk: {e}")
    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    asyncio.run(main())
