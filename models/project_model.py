from sqlalchemy import Boolean, Column, Integer, String, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base

class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    title = Column(String, nullable=False)
    industry = Column(String, nullable=True)
    sub_industry = Column(String, nullable=True)
    country = Column(String, nullable=True)
    currency = Column(String, nullable=True)
    location = Column(String, nullable=True)
    promoter_name = Column(String, nullable=True)
    promoter_experience = Column(String, nullable=True)
    project_description = Column(String, nullable=True)
    target_market = Column(String, nullable=True)
    target_customers = Column(String, nullable=True)
    project_cost = Column(Float, nullable=True)
    own_contribution = Column(Float, nullable=True)
    loan_amount = Column(Float, nullable=True)
    purpose = Column(String, nullable=True)
    government_scheme_name = Column(String, nullable=True)
    report_format = Column(String, nullable=True)
    financial_format = Column(String, nullable=True)
    status = Column(String, default="draft")
    # A one-time report credit (Entrepreneur) was spent on this project. Keeps a failed first
    # generation retryable without spending a second credit, and keeps credit-paid projects
    # out of a Consultant plan's per-cycle count.
    paid_with_credit = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    user = relationship("User", back_populates="projects")
    # Deleting a project removes its dependent rows (their FKs are NOT NULL,
    # so without a cascade SQLAlchemy would try to null them and fail).
    report = relationship("Report", back_populates="project", uselist=False, cascade="all, delete-orphan")
    questionnaire = relationship("QuestionnaireAnswer", back_populates="project", uselist=False, cascade="all, delete-orphan")
    scores = relationship("FeasibilityScore", back_populates="project", uselist=False, cascade="all, delete-orphan")
