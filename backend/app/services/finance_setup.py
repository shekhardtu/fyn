from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..event_time import now_utc
from ..finance_schemas import MAX_FINANCE_AMOUNT_MINOR, AccountCreateIn, BudgetSaveIn, GoalSaveIn
from ..models import Account, AccountBalanceSnapshot, Budget, Goal, GoalContribution, InvestmentHolding, Loan, TransactionDraft, User
from .repositories import UserScopedRepository
from .taxonomy import TaxonomyRepository
from .transactions import detach_account_transactions


class FinanceConflict(ValueError):
    pass


def _lock_owner(db: Session, user: User) -> None:
    # Serialize direct writes for this owner, including duplicate checks.
    db.scalar(select(User).where(User.id == user.id).with_for_update())


def save_budget(db: Session, user: User, values: BudgetSaveIn, budget_id: UUID | None = None) -> Budget:
    _lock_owner(db, user)
    budget = db.scalar(select(Budget).where(Budget.id == budget_id, Budget.user_id == user.id)) if budget_id else None
    if budget_id and budget is None:
        raise LookupError("Unknown budget")
    if values.category_id and not TaxonomyRepository(db, user.id).category(values.category_id, expense_only=True):
        raise ValueError("Choose an available expense category.")
    if budget and budget.category_id != values.category_id:
        raise ValueError("An existing budget's category cannot be changed.")
    if budget is None:
        existing = db.scalar(select(Budget).where(Budget.user_id == user.id, Budget.currency == user.currency, Budget.period == "monthly", Budget.category_id == values.category_id))
        if existing:
            raise FinanceConflict("A monthly budget already exists for this scope. Open that budget to edit its limit.")
        budget = Budget(user_id=user.id, currency=user.currency, category_id=values.category_id, period="monthly")
        db.add(budget)
    budget.name = values.name
    budget.amount_minor = values.amount_minor
    db.flush()
    return budget


def save_goal(db: Session, user: User, values: GoalSaveIn, goal_id: UUID | None = None) -> Goal:
    _lock_owner(db, user)
    goal = db.scalar(select(Goal).where(Goal.id == goal_id, Goal.user_id == user.id)) if goal_id else None
    if goal_id and goal is None:
        raise LookupError("Unknown goal")
    duplicate = db.scalar(select(Goal).where(Goal.user_id == user.id, Goal.currency == (goal.currency if goal else user.currency), func.lower(Goal.name) == values.name.lower()))
    if duplicate and duplicate is not goal:
        raise FinanceConflict("A goal with this name already exists. Edit that goal or choose another name.")
    if goal is None:
        goal = Goal(user_id=user.id, currency=user.currency, current_minor=0)
        db.add(goal)
    goal.name = values.name
    goal.target_minor = values.target_minor
    goal.target_date = values.target_date
    db.flush()
    return goal


def contribute_to_goal(db: Session, user: User, goal_id: UUID, amount_minor: int, request_id: UUID) -> Goal:
    _lock_owner(db, user)
    goal = db.scalar(select(Goal).where(Goal.id == goal_id, Goal.user_id == user.id).with_for_update())
    if goal is None:
        raise LookupError("Unknown goal")
    previous = UserScopedRepository(db, user.id).get(GoalContribution, request_id)
    if previous:
        if previous.user_id != user.id or previous.goal_id != goal.id or previous.amount_minor != amount_minor:
            raise FinanceConflict("This savings entry was already recorded with different details. Reopen the form to add another entry.")
        return goal
    if goal.current_minor + amount_minor > MAX_FINANCE_AMOUNT_MINOR:
        raise ValueError(f"Total recorded savings cannot exceed {MAX_FINANCE_AMOUNT_MINOR / 100:,.2f} {goal.currency}.")
    try:
        # A caller-supplied key may already belong to another owner. Let the
        # unique constraint reject that without reading the other owner's row
        # or changing this goal's progress.
        with db.begin_nested():
            db.add(GoalContribution(id=request_id, user_id=user.id, goal_id=goal.id, amount_minor=amount_minor, currency=goal.currency, contribution_at=now_utc()))
            db.flush()
    except IntegrityError as error:
        raise FinanceConflict("This savings entry key is unavailable. Reopen the form to add another entry.") from error
    goal.current_minor += amount_minor
    db.flush()
    return goal


def create_account(db: Session, user: User, values: AccountCreateIn) -> Account:
    _lock_owner(db, user)
    if db.scalar(select(Account).where(Account.user_id == user.id, func.lower(Account.name) == values.name.lower())):
        raise FinanceConflict("An account with this name already exists. Choose a different name.")
    account = Account(user_id=user.id, **values.model_dump())
    db.add(account)
    db.flush()
    db.add(AccountBalanceSnapshot(user_id=user.id, account_id=account.id, balance_minor=account.balance_minor, currency=account.currency, observed_at=now_utc(), source_type="manual"))
    db.flush()
    return account


def delete_account(db: Session, user: User, account_id: UUID) -> None:
    _lock_owner(db, user)
    account = db.scalar(select(Account).where(Account.id == account_id, Account.user_id == user.id).with_for_update())
    if account is None:
        raise LookupError("Unknown account")
    detach_account_transactions(db, user.id, account.id)
    # Clear draft names too, so resuming a draft cannot recreate the account.
    db.execute(update(TransactionDraft).where(TransactionDraft.user_id == user.id, TransactionDraft.account_id == account.id).values(account_id=None, source_account_name=None))
    db.execute(update(TransactionDraft).where(TransactionDraft.user_id == user.id, TransactionDraft.destination_account_id == account.id).values(destination_account_id=None, destination_account_name=None))
    for model in (InvestmentHolding, Loan):
        db.execute(update(model).where(model.user_id == user.id, model.account_id == account.id).values(account_id=None))
    db.execute(delete(AccountBalanceSnapshot).where(AccountBalanceSnapshot.user_id == user.id, AccountBalanceSnapshot.account_id == account.id))
    db.delete(account)
    db.flush()
