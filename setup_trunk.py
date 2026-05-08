"""Update an existing LiveKit outbound SIP trunk with current .env credentials.

Useful after rotating Vobiz credentials or moving providers without
recreating the trunk ID.
"""

import asyncio
import os
import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from dotenv import load_dotenv
from livekit import api

import config

load_dotenv(".env")


async def main():
    trunk_id = config.SIP_TRUNK_ID
    if not trunk_id:
        print("Error: SIP_TRUNK_ID not set in .env")
        return

    print(f"Updating SIP trunk: {trunk_id}")
    print(f"  Address:  {config.SIP_DOMAIN}")
    print(f"  Username: {config.SIP_USERNAME}")
    print(f"  Numbers:  [{config.SIP_OUTBOUND_NUMBER}]")

    lkapi = api.LiveKitAPI(
        url=config.LIVEKIT_URL,
        api_key=config.LIVEKIT_API_KEY,
        api_secret=config.LIVEKIT_API_SECRET,
    )
    try:
        await lkapi.sip.update_outbound_trunk_fields(
            trunk_id,
            address=config.SIP_DOMAIN,
            auth_username=config.SIP_USERNAME,
            auth_password=config.SIP_PASSWORD,
            numbers=[config.SIP_OUTBOUND_NUMBER] if config.SIP_OUTBOUND_NUMBER else [],
        )
        print("\nSIP trunk updated.")
    except Exception as e:
        print(f"\nFailed to update trunk: {e}")
    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    asyncio.run(main())
