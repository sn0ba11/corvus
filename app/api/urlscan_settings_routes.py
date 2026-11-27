from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.security import verify_csrf
from app.db.session import get_db
from app.models.db import AuditEvent
from app.services.urlscan_settings import (
    clear_urlscan_api_key,
    save_urlscan_api_key,
    set_urlscan_adapter_enabled,
)

router = APIRouter()


@router.post("/settings/urlscan-api-key")
async def update_urlscan_api_key_setting(
    request: Request, db: Session = Depends(get_db)
) -> Response:
    form = await request.form()
    supplied = str(form.get("csrf", ""))
    if not verify_csrf(request.session.get("csrf"), supplied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")

    action = str(form.get("action", "save"))
    if action == "clear":
        clear_urlscan_api_key(db)
        set_urlscan_adapter_enabled(False)
        event_type = "urlscan_api_key_cleared"
        destination = "/settings?urlscan_cleared=1"
    elif action == "save":
        try:
            save_urlscan_api_key(db, form.get("urlscan_api_key"))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        set_urlscan_adapter_enabled(True)
        event_type = "urlscan_api_key_updated"
        destination = "/settings?urlscan_saved=1"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid settings action")

    db.add(
        AuditEvent(
            investigation_id=None,
            event_type=event_type,
            details={"configured": action == "save"},
        )
    )
    db.commit()
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
