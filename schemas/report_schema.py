from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class ReportCreate(BaseModel):
    report_content: str
    report_format: Optional[str] = None
    financial_format: Optional[str] = None

class ReportResponse(BaseModel):
    id: int
    project_id: int
    report_content: Optional[str] = None
    report_format: Optional[str] = None
    financial_format: Optional[str] = None
    status: Optional[str] = None
    created_at: datetime
    # Whether the written (Word) report has been generated — the UI offers "Create Word
    # report" until it has.
    word_report: bool = False

    class Config:
        from_attributes = True