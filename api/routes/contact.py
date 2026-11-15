"""Contact form endpoint — stores messages in Supabase for admin review."""
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from api.supabase_client import supabase_post

router = APIRouter()


class ContactRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=10, max_length=2000)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', v):
            raise ValueError("Invalid email address")
        return v

    @field_validator("name", "message")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class ContactResponse(BaseModel):
    success: bool
    error: str | None = None


@router.post("/api/contact", response_model=ContactResponse, status_code=201)
def submit_contact(req: ContactRequest):
    """Submit a contact message. No authentication required."""
    try:
        supabase_post("contact_messages", {
            "name": req.name,
            "email": req.email,
            "message": req.message,
        })
        return ContactResponse(success=True)
    except Exception:  # noqa: BLE001 — any Supabase transport failure translates to a 500 the user can retry; error type doesn't matter to the caller
        # `from None` suppresses the exception chain in server logs — the
        # user-facing 500 already carries the actionable message and the
        # inner traceback would just be Supabase noise.
        raise HTTPException(status_code=500, detail="Failed to send message. Please try again.") from None
