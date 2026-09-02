from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from services.claude_service import invoke_llm
from services.entitlements import may_generate
from models.user_model import User
from models.project_model import Project
from database import get_db
from dependencies import get_current_user

router = APIRouter(prefix="/ai", tags=["AI"])

class LLMRequest(BaseModel):
    prompt: str
    model: str = "claude_sonnet_4_6"
    # Set by the report chat once a project exists (e.g. regenerating an already-generated
    # report's narrative). Left unset for a brand-new report conversation — see the plan
    # check below, which treats "no project_id" as "this would be a NEW report".
    project_id: int | None = None

class LLMResponse(BaseModel):
    text: str

# The chat that drafts a report (CreateReport.jsx's "Report Assistant") talks to this
# endpoint on every message, with the whole system prompt + conversation built client-side —
# this route has no idea what it's being asked until the text arrives. Before entitlements
# were checked here, a free user who had used their one report could keep chatting and, by
# insisting, get the model to write the full report as plain chat text: the actual
# /generate/{project_id} pipeline was gated, but this open door to the same model was not,
# so the paywall was cosmetic. Applying the SAME plan check used by /generate — allowed for
# an existing project (a regeneration of something already paid for), blocked for a brand
# new one once the plan's report quota is spent — closes that gap without special-casing the
# report-drafting prompt itself. On a block we return a normal 200 with a friendly message
# instead of an error, so it lands in the chat as the assistant's reply with no frontend
# change needed.
UPGRADE_MESSAGE = (
    "You've used up what your current plan covers, so I can't put together another report "
    "here for free — sorry about that! Please upgrade at /pricing, and the moment you're on "
    "a paid plan, come straight back — I'll pick up right where we left off and build it "
    "for you."
)

@router.post("/invoke", response_model=LLMResponse)
def invoke(request: LLMRequest, current_user: User = Depends(get_current_user),
           db: Session = Depends(get_db)):
    if request.project_id is not None:
        owned = db.query(Project.id).filter(
            Project.id == request.project_id, Project.user_id == current_user.id
        ).first()
        project_id = request.project_id if owned else None
    else:
        project_id = None

    allowed, _ = may_generate(db, current_user, project_id)
    if not allowed:
        return LLMResponse(text=UPGRADE_MESSAGE)

    try:
        result = invoke_llm(prompt=request.prompt, model=request.model)
        return LLMResponse(text=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
