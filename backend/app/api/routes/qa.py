from fastapi import APIRouter

from app.models.schemas import AnswerOut, QuestionIn
from app.services.qa import answer_question

router = APIRouter()


@router.post("/qa", response_model=AnswerOut)
async def ask(payload: QuestionIn) -> dict:
    return await answer_question(payload.question, payload.dataset_id)

