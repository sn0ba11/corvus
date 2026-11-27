from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.security import verify_csrf
from app.db.session import get_db
from app.models.db import AuditEvent
from app.services.external_api_settings import (
    clear_abuseipdb_api_key,
    clear_companies_house_api_key,
    clear_openphish_feed_url,
    save_abuseipdb_api_key,
    save_companies_house_api_key,
    save_openphish_feed_url,
    set_companies_house_adapter_enabled,
    set_openphish_adapter_enabled,
)

router = APIRouter()


def _check_csrf(request: Request, supplied: str) -> None:
    if not verify_csrf(request.session.get("csrf"), supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")


@router.post("/settings/companies-house-api-key")
async def update_companies_house_api_key(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    action = str(form.get("action", "save"))
    if action == "clear":
        clear_companies_house_api_key(db)
        set_companies_house_adapter_enabled(False)
        event_type = "companies_house_api_key_cleared"
        destination = "/settings?companies_house_cleared=1"
    elif action == "save":
        try:
            save_companies_house_api_key(db, form.get("companies_house_api_key"))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        set_companies_house_adapter_enabled(True)
        event_type = "companies_house_api_key_updated"
        destination = "/settings?companies_house_saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")
    db.add(AuditEvent(investigation_id=None, event_type=event_type, details={"configured": action == "save"}))
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/openphish-feed")
async def update_openphish_feed_setting(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    action = str(form.get("action", "save"))
    if action == "clear":
        clear_openphish_feed_url(db)
        set_openphish_adapter_enabled(False)
        event_type = "openphish_feed_cleared"
        destination = "/settings?openphish_cleared=1"
    elif action == "save":
        try:
            save_openphish_feed_url(db, form.get("openphish_feed_url"))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        set_openphish_adapter_enabled(True)
        event_type = "openphish_feed_updated"
        destination = "/settings?openphish_saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")
    db.add(AuditEvent(investigation_id=None, event_type=event_type, details={"configured": action == "save"}))
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/abuseipdb-api-key")
async def update_abuseipdb_api_key(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    _check_csrf(request, str(form.get("csrf", "")))
    action = str(form.get("action", "save"))
    if action == "clear":
        clear_abuseipdb_api_key(db)
        event_type = "abuseipdb_api_key_cleared"
        destination = "/settings?abuseipdb_cleared=1"
    elif action == "save":
        try:
            save_abuseipdb_api_key(db, form.get("abuseipdb_api_key"))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        event_type = "abuseipdb_api_key_updated"
        destination = "/settings?abuseipdb_saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")
    db.add(AuditEvent(investigation_id=None, event_type=event_type, details={"configured": action == "save"}))
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
