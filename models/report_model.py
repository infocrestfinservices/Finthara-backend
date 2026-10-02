import json

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    report_content = Column(Text, nullable=True)
    report_format = Column(String, nullable=True)
    financial_format = Column(String, nullable=True)
    financial_model = Column(Text, nullable=True)   # structured JSON for Word/Excel
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="report")

    # Narrative sections the Word download fills by itself into a workbook-only model and
    # saves back. A narrative with anything BEYOND these came from the full narrative call.
    _DOWNLOAD_FILLED_SECTIONS = {"Business Model", "Executive Summary"}

    @property
    def word_report(self) -> bool:
        """Has this report's written (Word) report been generated? The generation run that
        writes it stamps "word_report"; older reports are recognised by their narrative."""
        try:
            model = json.loads(self.financial_model or "{}")
        except (ValueError, TypeError):
            return False
        if model.get("word_report"):
            return True
        return any(k not in self._DOWNLOAD_FILLED_SECTIONS for k in (model.get("narrative") or {}))
