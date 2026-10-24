import logging
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("urja_api")

BASE_URL = "https://urja-ops.flockenergy.tech"
LOGIN_URL = f"{BASE_URL}/login"
SEARCH_URL = f"{BASE_URL}/portal/meters/search"

# Models
class MeterSummary(BaseModel):
    meterId: str
    serialNo: str
    make: str
    phaseType: str
    installStatus: str
    dtCode: str

class MeterSearchResponse(BaseModel):
    data: List[MeterSummary]

class ConsumptionPoint(BaseModel):
    timestamp: str
    kwh: float
    kvah: float
    voltR: float

class MeterDetail(BaseModel):
    meterId: str
    serialNo: str
    make: str
    phaseType: str
    installStatus: str
    dtCode: str
    location: Optional[Dict[str, float]] = None
    hierarchy: Optional[List[str]] = None
    consumption: Optional[List[ConsumptionPoint]] = None


class MeterEnergyResponse(BaseModel):
    data: List[ConsumptionPoint]


class PortalClient:
    """HTTP Client managing session state and SvelteKit authentication."""
    def __init__(self, username: str = "operator@urja.local", password: str = "urja-ops-2026"):
        self.username = username
        self.password = password
        self.client = httpx.AsyncClient(base_url=BASE_URL, follow_redirects=True, timeout=15.0)
        self.is_authenticated = False

    async def authenticate(self):
        """Performs 2-step SvelteKit login to satisfy CSRF checks."""
        logger.info("Attempting login to legacy portal...")
        
        # Step 1: Fetch login page to initialize CSRF session cookies
        get_res = await self.client.get("/login")
        if get_res.status_code != 200:
            raise RuntimeError(f"Failed to load login page: {get_res.status_code}")

        # Step 2: Submit form with required SvelteKit Origin/Referer headers
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE_URL,
            "Referer": LOGIN_URL,
        }
        payload = {
            "email": self.username,
            "password": self.password,
        }

        post_res = await self.client.post("/login", data=payload, headers=headers)
        
        # 200 OK or 303 Redirect indicates successful session creation
        if post_res.status_code in (200, 303) or "/meters" in str(post_res.url):
            self.is_authenticated = True
            logger.info("Successfully authenticated with Urja portal.")
        else:
            logger.error(f"Login failed. Status: {post_res.status_code}, Body: {post_res.text}")
            raise RuntimeError("Authentication failed with legacy portal.")

    async def get_meters(self, query: str = "", page: int = 1) -> Dict[str, Any]:
        """Fetches list of meters from portal JSON API."""
        if not self.is_authenticated:
            await self.authenticate()

        res = await self.client.get(
            "/portal/meters/search",
            params={"q": query, "page": page},
            headers={"Accept": "application/json, text/plain, */*"}
        )

        if res.status_code == 401 or "login" in str(res.url):
            # Session expired, re-authenticate and retry once
            logger.warning("Session expired. Re-authenticating...")
            await self.authenticate()
            res = await self.client.get(
                "/portal/meters/search",
                params={"q": query, "page": page},
                headers={"Accept": "application/json, text/plain, */*"}
            )

        if res.status_code != 200:
            raise HTTPException(status_code=res.status_code, detail="Failed to fetch meter search results from upstream.")

        return res.json()

    async def get_meter_details(self, meter_id: str) -> Dict[str, Any]:
        """Fetches detailed meter metadata and consumption history.

        The upstream portal exposes meter detail data under the /energy variant for
        these records, even though the same meter ID is valid under the base URL.
        """
        if not self.is_authenticated:
            await self.authenticate()

        candidate_paths = [
            f"/portal/meters/{meter_id}/energy",
            f"/portal/meters/{meter_id}",
        ]

        for path in candidate_paths:
            res = await self.client.get(
                path,
                headers={"Accept": "application/json, text/plain, */*"}
            )

            if res.status_code == 401 or "login" in str(res.url):
                await self.authenticate()
                res = await self.client.get(
                    path,
                    headers={"Accept": "application/json, text/plain, */*"}
                )

            if res.status_code == 200:
                return res.json()

            if res.status_code != 404:
                raise HTTPException(status_code=res.status_code, detail="Failed to fetch meter details from upstream.")

        raise HTTPException(status_code=404, detail=f"Meter '{meter_id}' not found.")

    async def close(self):
        await self.client.aclose()


portal_client = PortalClient()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Auto-login when API service starts up
    try:
        await portal_client.authenticate()
    except Exception as e:
        logger.error(f"Startup authentication warning: {e}")
    yield
    await portal_client.close()

app = FastAPI(
    title="Urja Meter Ops REST API",
    description="Clean, documented wrapper service providing programmatic access to Urja Meter Ops legacy portal.",
    version="1.0.0",
    lifespan=lifespan
)

@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "upstream_authenticated": portal_client.is_authenticated}

@app.get("/api/v1/meters", response_model=MeterSearchResponse, tags=["Meters"])
async def list_meters(
    q: str = Query("", description="Search term for meter ID or serial number"),
    page: int = Query(1, ge=1, description="Page number")
):
    """
    Search and paginate through electricity meters.
    """
    data = await portal_client.get_meters(query=q, page=page)
    return data

@app.get("/api/v1/meters/{meter_id}", tags=["Meters"])
async def get_meter_detail(meter_id: str):
    """
    Retrieve full details, network hierarchy placement, and consumption history for a single meter.
    """
    data = await portal_client.get_meter_details(meter_id=meter_id)
    return data


@app.get("/api/v1/meters/{meter_id}/energy", response_model=MeterEnergyResponse, tags=["Meters"])
async def get_meter_energy(meter_id: str):
    """
    Retrieve the meter's energy consumption series from the legacy portal.
    """
    data = await portal_client.get_meter_details(meter_id=meter_id)
    return data