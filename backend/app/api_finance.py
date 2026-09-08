from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .finance_schemas import AccountCreateIn, AccountRecordOut, BudgetRecordOut, BudgetSaveIn, GoalContributionIn, GoalRecordOut, GoalSaveIn
from .models import Account, Budget, Goal, User
from .security import current_user
from .services import finance_setup

router = APIRouter()


def _failure(error: ValueError | LookupError) -> HTTPException:
    code = 404 if isinstance(error, LookupError) else 409 if isinstance(error, finance_setup.FinanceConflict) else 422
    return HTTPException(status_code=code, detail=str(error))


@router.get("/budgets", response_model=list[BudgetRecordOut])
def list_budgets(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return list(db.scalars(select(Budget).where(Budget.user_id == user.id, Budget.period == "monthly").order_by(Budget.name)))


@router.post("/budgets", response_model=BudgetRecordOut, status_code=201)
def create_budget(request: BudgetSaveIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        budget = finance_setup.save_budget(db, user, request)
    except ValueError as error:
        raise _failure(error) from error
    db.commit()
    return budget


@router.patch("/budgets/{budget_id}", response_model=BudgetRecordOut)
def update_budget(budget_id: UUID, request: BudgetSaveIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        budget = finance_setup.save_budget(db, user, request, budget_id)
    except (ValueError, LookupError) as error:
        raise _failure(error) from error
    db.commit()
    return budget


@router.get("/goals", response_model=list[GoalRecordOut])
def list_goals(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return list(db.scalars(select(Goal).where(Goal.user_id == user.id).order_by(Goal.created_at.desc())))


@router.post("/goals", response_model=GoalRecordOut, status_code=201)
def create_goal(request: GoalSaveIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        goal = finance_setup.save_goal(db, user, request)
    except ValueError as error:
        raise _failure(error) from error
    db.commit()
    return goal


@router.patch("/goals/{goal_id}", response_model=GoalRecordOut)
def update_goal(goal_id: UUID, request: GoalSaveIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        goal = finance_setup.save_goal(db, user, request, goal_id)
    except (ValueError, LookupError) as error:
        raise _failure(error) from error
    db.commit()
    return goal


@router.post("/goals/{goal_id}/contributions", response_model=GoalRecordOut, status_code=201)
def contribute_goal(goal_id: UUID, request: GoalContributionIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        goal = finance_setup.contribute_to_goal(db, user, goal_id, request.amount_minor, request.request_id)
    except (ValueError, LookupError) as error:
        raise _failure(error) from error
    db.commit()
    return goal


@router.get("/accounts", response_model=list[AccountRecordOut])
def list_accounts(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return list(db.scalars(select(Account).where(Account.user_id == user.id).order_by(Account.name)))


@router.post("/accounts", response_model=AccountRecordOut, status_code=201)
def create_account(request: AccountCreateIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        account = finance_setup.create_account(db, user, request)
    except ValueError as error:
        raise _failure(error) from error
    db.commit()
    return account
