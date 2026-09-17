from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from agents.market_research_agent import market_research_agent
from agents.feasibility_agent import feasibility_agent
from agents.swot_agent import swot_agent
from agents.report_agent import report_agent
from models.user_model import User
from dependencies import get_current_user

router = APIRouter(prefix="/analysis", tags=["Analysis"])

class AnalysisRequest(BaseModel):
    business_name: str
    industry: str
    country: str
    purpose: str
    description: str
    project_cost: Optional[float] = 0
    own_contribution: Optional[float] = 0
    loan_amount: Optional[float] = 0

class AnalysisResponse(BaseModel):
    market_research: str
    feasibility: str
    swot: str
    report: str

@router.post("/full", response_model=AnalysisResponse)
def run_full_analysis(request: AnalysisRequest, current_user: User = Depends(get_current_user)):
    try:
        market = market_research_agent(
            business_name=request.business_name,
            industry=request.industry,
            country=request.country,
            purpose=request.purpose,
            description=request.description
        )
        feasibility = feasibility_agent(
            business_name=request.business_name,
            industry=request.industry,
            country=request.country,
            description=request.description,
            project_cost=request.project_cost,
            own_contribution=request.own_contribution,
            loan_amount=request.loan_amount
        )
        swot = swot_agent(
            business_name=request.business_name,
            industry=request.industry,
            country=request.country,
            description=request.description
        )
        report = report_agent(
            business_name=request.business_name,
            industry=request.industry,
            country=request.country,
            purpose=request.purpose,
            description=request.description,
            market_research=market,
            feasibility=feasibility,
            swot=swot,
            project_cost=request.project_cost,
            own_contribution=request.own_contribution,
            loan_amount=request.loan_amount
        )
        return AnalysisResponse(
            market_research=market,
            feasibility=feasibility,
            swot=swot,
            report=report
        )
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))

@router.post("/market")
def run_market_research(request: AnalysisRequest, current_user: User = Depends(get_current_user)):
    try:
        result = market_research_agent(
            business_name=request.business_name,
            industry=request.industry,
            country=request.country,
            purpose=request.purpose,
            description=request.description
        )
        return {"result": result}
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))

@router.post("/feasibility")
def run_feasibility(request: AnalysisRequest, current_user: User = Depends(get_current_user)):
    try:
        result = feasibility_agent(
            business_name=request.business_name,
            industry=request.industry,
            country=request.country,
            description=request.description,
            project_cost=request.project_cost,
            own_contribution=request.own_contribution,
            loan_amount=request.loan_amount
        )
        return {"result": result}
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))

@router.post("/swot")
def run_swot(request: AnalysisRequest, current_user: User = Depends(get_current_user)):
    try:
        result = swot_agent(
            business_name=request.business_name,
            industry=request.industry,
            country=request.country,
            description=request.description
        )
        return {"result": result}
    except Exception as e:
        raise HTTPException(status_code=409, detail=str(e))