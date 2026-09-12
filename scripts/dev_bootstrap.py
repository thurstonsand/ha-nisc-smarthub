"""Onboard the local Home Assistant dev instance and mint an agent token.

Home Assistant has no supported way to print a token, and the documented
long-lived token command needs an authenticated user. A fresh instance will
however create its owner over the onboarding API the frontend uses, and hand
back an authorization code. This walks that path: create the owner, exchange
the code, finish the remaining onboarding steps, then mint a long-lived token
over the WebSocket API and write it to `config/.agent-token`.

That onboarding API is internal and version-pinned. When a Home Assistant
upgrade breaks this script, fix the script.

Run it against a running instance:

    mise run dev:bootstrap
"""

import asyncio
from pathlib import Path
from typing import Any, Final

import aiohttp

HOST: Final = "127.0.0.1:8123"
BASE: Final = f"http://{HOST}"
CLIENT_ID: Final = f"{BASE}/"
OWNER_NAME: Final = "Dev"
OWNER_USERNAME: Final = "dev"
OWNER_PASSWORD: Final = "development"
TOKEN_FILE: Final = Path("config") / ".agent-token"
STARTUP_TIMEOUT: Final = 180.0


async def wait_for_server(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    """Wait for the instance to answer, and return its onboarding status."""
    deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT
    while True:
        try:
            async with session.get(f"{BASE}/api/onboarding") as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError:
            if asyncio.get_running_loop().time() > deadline:
                raise SystemExit(
                    f"no Home Assistant answered on {HOST} within "
                    f"{STARTUP_TIMEOUT:.0f}s; start `mise run dev` first"
                ) from None
            await asyncio.sleep(2)


async def post(
    session: aiohttp.ClientSession,
    path: str,
    payload: dict[str, str] | None = None,
    token: str | None = None,
) -> Any:
    """POST one onboarding step and return its JSON answer."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with session.post(
        f"{BASE}{path}", json=payload or {}, headers=headers
    ) as response:
        body = await response.text()
        if response.status >= 400:
            raise SystemExit(f"{path} failed with HTTP {response.status}: {body}")
        return await response.json()


async def exchange(session: aiohttp.ClientSession, auth_code: str) -> str:
    """Exchange an onboarding authorization code for an access token."""
    async with session.post(
        f"{BASE}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": CLIENT_ID,
        },
    ) as response:
        body = await response.json()
        if response.status >= 400:
            raise SystemExit(f"token exchange failed: {body}")
        return body["access_token"]


async def mint(session: aiohttp.ClientSession, access_token: str) -> str:
    """Mint a long-lived token over the WebSocket API."""
    async with session.ws_connect(f"ws://{HOST}/api/websocket") as socket:
        await socket.receive_json()
        await socket.send_json({"type": "auth", "access_token": access_token})
        authenticated = await socket.receive_json()
        if authenticated["type"] != "auth_ok":
            raise SystemExit(f"websocket refused the access token: {authenticated}")

        await socket.send_json(
            {
                "id": 1,
                "type": "auth/long_lived_access_token",
                "client_name": "nisc-smarthub-local-agent",
                "lifespan": 365,
            }
        )
        result = await socket.receive_json()
        if not result["success"]:
            raise SystemExit(f"could not mint a long-lived token: {result}")
        return result["result"]


async def bootstrap() -> None:
    """Onboard the instance if it is fresh, and write the agent token."""
    if TOKEN_FILE.exists():
        print(f"{TOKEN_FILE} already exists; nothing to do")
        return

    async with aiohttp.ClientSession() as session:
        status = await wait_for_server(session)
        done = {step["step"] for step in status if step["done"]}
        if "user" in done:
            raise SystemExit(
                f"this instance is already onboarded but {TOKEN_FILE} is gone; "
                "delete config/ to start over, or mint a token in the UI"
            )

        created = await post(
            session,
            "/api/onboarding/users",
            {
                "name": OWNER_NAME,
                "username": OWNER_USERNAME,
                "password": OWNER_PASSWORD,
                "client_id": CLIENT_ID,
                "language": "en",
            },
        )
        access_token = await exchange(session, created["auth_code"])

        await post(session, "/api/onboarding/core_config", token=access_token)
        await post(session, "/api/onboarding/analytics", token=access_token)
        await post(
            session,
            "/api/onboarding/integration",
            {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID},
            token=access_token,
        )

        token = await mint(session, access_token)

    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)
    print(f"onboarded {HOST} as {OWNER_USERNAME}/{OWNER_PASSWORD}")
    print(f"wrote an agent token to {TOKEN_FILE}")


if __name__ == "__main__":
    asyncio.run(bootstrap())
