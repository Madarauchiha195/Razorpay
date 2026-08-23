from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Integer, JSON, String, create_engine, desc, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from ..domain.catalog import DEFAULT_PRODUCT_ID, SEED_CATALOG
from ..domain.models import MerchantPolicy, utc_now


class Base(DeclarativeBase):
    pass


class PolicyRow(Base):
    """One row per product. product_id is the business key the API and agents route on."""

    __tablename__ = "merchant_policies"
    product_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SessionRow(Base):
    __tablename__ = "negotiation_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(80))
    product_id: Mapped[str] = mapped_column(String(80))
    intent: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EventRow(Base):
    __tablename__ = "policy_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(20))
    decision_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    message: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class DealRow(Base):
    __tablename__ = "authorized_deals"
    deal_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    authorization_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    signature: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PaymentRow(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deal_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    provider_order_id: Mapped[str] = mapped_column(String(128), unique=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RiskStateRow(Base):
    __tablename__ = "risk_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    violation_count: Mapped[int] = mapped_column(Integer, default=0)
    daily_concession_cost: Mapped[int] = mapped_column(Integer, default=0)
    transactions_today: Mapped[int] = mapped_column(Integer, default=0)
    # The counters above are per-day. usage_date records which day they belong to so they
    # can roll over instead of accumulating forever and permanently blocking the agent.
    usage_date: Mapped[date] = mapped_column(Date, default=lambda: utc_now().date())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AuditRow(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(96))
    decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DealMeshRepository:
    def __init__(self, database_url: str):
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        engine_options: dict[str, Any] = {"connect_args": connect_args, "future": True}
        # This allows the FastAPI test client and local test suite to safely share one in-memory database.
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **engine_options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialise(self) -> None:
        self._migrate_legacy_schema()
        Base.metadata.create_all(self.engine)
        with self.sessions.begin() as db:
            existing = set(db.scalars(select(PolicyRow.product_id)).all())
            for order, policy in enumerate(SEED_CATALOG):
                if policy.product_id not in existing:
                    db.add(PolicyRow(
                        product_id=policy.product_id, version=1, sort_order=order,
                        data=policy.model_dump(mode="json"),
                    ))
            if db.get(RiskStateRow, 1) is None:
                db.add(RiskStateRow(id=1, usage_date=utc_now().date()))

    def _migrate_legacy_schema(self) -> None:
        """Bring a pre-multi-product database forward without losing negotiation history.

        The original schema stored a single policy at merchant_policies.id == 1 and had no
        usage_date on risk_state. Policy rows are seed/config data so they are safe to
        rebuild; sessions, deals, payments, events and audit rows are left untouched.
        """
        inspector = inspect(self.engine)
        if not inspector.has_table("merchant_policies"):
            pass
        else:
            columns = {column["name"] for column in inspector.get_columns("merchant_policies")}
            if "product_id" not in columns:
                with self.engine.begin() as connection:
                    connection.execute(text("DROP TABLE merchant_policies"))

        if inspector.has_table("risk_state"):
            columns = {column["name"] for column in inspector.get_columns("risk_state")}
            if "usage_date" not in columns:
                with self.engine.begin() as connection:
                    connection.execute(text("ALTER TABLE risk_state ADD COLUMN usage_date DATE"))
                    # The pre-migration counters had no rollover, so they are an unbounded running
                    # total rather than today's usage. Carrying them forward would start the day
                    # already near the circuit breaker - and a violation count at or above the
                    # freeze threshold would leave the agent frozen with no way back.
                    connection.execute(
                        text(
                            "UPDATE risk_state SET usage_date = :today, daily_concession_cost = 0,"
                            " transactions_today = 0, violation_count = 0, agent_frozen = 0"
                        ),
                        {"today": utc_now().date().isoformat()},
                    )

    def list_products(self) -> list[dict[str, Any]]:
        """Public catalog view. Merchant-confidential fields are deliberately excluded."""
        with self.sessions() as db:
            rows = db.scalars(select(PolicyRow).order_by(PolicyRow.sort_order, PolicyRow.product_id)).all()
            products: list[dict[str, Any]] = []
            for row in rows:
                policy = MerchantPolicy.model_validate(row.data)
                products.append({
                    "product_id": policy.product_id,
                    "product_name": policy.product_name,
                    "listing_price": policy.target_price,
                    "flagship": policy.flagship_product,
                    "policy_version": policy.policy_version,
                    "concessions": [
                        {"id": item.id, "name": item.name, "customer_value": item.customer_perceived_value}
                        for item in policy.concessions if item.allowed and item.inventory_available
                    ],
                })
            return products

    def get_policy(self, product_id: str | None = None) -> MerchantPolicy:
        with self.sessions() as db:
            row = db.get(PolicyRow, product_id) if product_id else None
            if row is None and product_id:
                raise KeyError(product_id)
            if row is None:
                row = db.scalar(select(PolicyRow).order_by(PolicyRow.sort_order, PolicyRow.product_id))
            if row is None:
                raise RuntimeError("Merchant catalog was not initialized")
            return MerchantPolicy.model_validate(row.data)

    def save_policy(self, policy: MerchantPolicy) -> MerchantPolicy:
        with self.sessions.begin() as db:
            row = db.get(PolicyRow, policy.product_id)
            next_version = (row.version if row else 0) + 1
            stored = policy.model_copy(update={"policy_version": f"v{next_version}"})
            if row is None:
                highest = db.scalar(select(func.max(PolicyRow.sort_order))) or 0
                db.add(PolicyRow(
                    product_id=stored.product_id, version=next_version,
                    sort_order=highest + 1, data=stored.model_dump(mode="json"),
                ))
            else:
                row.version = next_version
                row.data = stored.model_dump(mode="json")
            return stored

    def create_session(self, session_id: str, customer_id: str, product_id: str, intent: dict[str, Any]) -> None:
        with self.sessions.begin() as db:
            db.add(SessionRow(id=session_id, customer_id=customer_id, product_id=product_id, intent=intent))

    def get_session(self, session_id: str) -> SessionRow | None:
        with self.sessions() as db:
            return db.get(SessionRow, session_id)

    def set_session_status(self, session_id: str, status: str) -> None:
        with self.sessions.begin() as db:
            row = db.get(SessionRow, session_id)
            if row:
                row.status = status

    def add_event(
        self, session_id: str | None, event_type: str, level: str, message: str,
        decision_code: str | None = None, payload: dict[str, Any] | None = None,
    ) -> None:
        with self.sessions.begin() as db:
            db.add(EventRow(
                session_id=session_id, event_type=event_type, level=level, message=message,
                decision_code=decision_code, payload=payload or {},
            ))

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.sessions() as db:
            records = db.scalars(select(EventRow).order_by(desc(EventRow.created_at)).limit(limit)).all()
            return [
                {
                    "id": row.id, "session_id": row.session_id, "event_type": row.event_type,
                    "level": row.level, "decision_code": row.decision_code, "message": row.message,
                    "payload": row.payload, "created_at": row.created_at.isoformat(),
                }
                for row in records
            ]

    @staticmethod
    def _roll_over(row: RiskStateRow) -> RiskStateRow:
        """Reset the per-day counters when the UTC date has changed.

        Without this the 'daily' budget accumulates forever and DealGuard eventually blocks
        every negotiation with no in-app way to recover.
        """
        today = utc_now().date()
        if row.usage_date != today:
            row.usage_date = today
            row.daily_concession_cost = 0
            row.transactions_today = 0
            row.violation_count = 0
        return row

    def get_usage(self) -> dict[str, int | bool]:
        with self.sessions.begin() as db:
            row = db.get(RiskStateRow, 1)
            if row is None:
                return {"agent_frozen": False, "violation_count": 0, "daily_concession_cost": 0, "transactions_today": 0}
            self._roll_over(row)
            return {
                "agent_frozen": row.agent_frozen, "violation_count": row.violation_count,
                "daily_concession_cost": row.daily_concession_cost, "transactions_today": row.transactions_today,
            }

    def reset_daily_usage(self) -> dict[str, int | bool]:
        """Operator escape hatch: clear the day's counters and unfreeze the agent."""
        with self.sessions.begin() as db:
            row = db.get(RiskStateRow, 1)
            if row is None:
                row = RiskStateRow(id=1)
                db.add(row)
            row.usage_date = utc_now().date()
            row.daily_concession_cost = 0
            row.transactions_today = 0
            row.violation_count = 0
            row.agent_frozen = False
            return {
                "agent_frozen": row.agent_frozen, "violation_count": row.violation_count,
                "daily_concession_cost": row.daily_concession_cost, "transactions_today": row.transactions_today,
            }

    def register_block(self) -> dict[str, int | bool]:
        with self.sessions.begin() as db:
            row = db.get(RiskStateRow, 1)
            assert row is not None
            self._roll_over(row)
            row.violation_count += 1
            if row.violation_count >= 5:
                row.agent_frozen = True
            return {"agent_frozen": row.agent_frozen, "violation_count": row.violation_count}

    def set_agent_frozen(self, frozen: bool) -> None:
        with self.sessions.begin() as db:
            row = db.get(RiskStateRow, 1)
            assert row is not None
            row.agent_frozen = frozen
            if not frozen:
                row.violation_count = 0

    def add_deal(self, data: dict[str, Any], status: str, signature: str, expires_at: datetime) -> None:
        with self.sessions.begin() as db:
            db.add(DealRow(
                deal_id=data["deal_id"], authorization_id=data["authorization_id"], status=status,
                signature=signature, expires_at=expires_at, data=data,
            ))

    def get_deal(self, deal_id: str) -> DealRow | None:
        with self.sessions() as db:
            return db.get(DealRow, deal_id)

    def update_deal_status(self, deal_id: str, status: str) -> None:
        with self.sessions.begin() as db:
            row = db.get(DealRow, deal_id)
            if row is None:
                raise KeyError(deal_id)
            row.status = status

    def create_payment(self, deal_id: str, key: str, provider_order_id: str) -> None:
        with self.sessions.begin() as db:
            db.add(PaymentRow(deal_id=deal_id, idempotency_key=key, provider_order_id=provider_order_id))
            deal = db.get(DealRow, deal_id)
            if deal is None:
                raise KeyError(deal_id)
            deal.status = "PAYMENT_CREATED"
            risk = db.get(RiskStateRow, 1)
            assert risk is not None
            self._roll_over(risk)
            risk.transactions_today += 1

    def get_payment(self, deal_id: str) -> PaymentRow | None:
        with self.sessions() as db:
            return db.scalar(select(PaymentRow).where(PaymentRow.deal_id == deal_id))

    def mark_paid(self, deal_id: str, payment_id: str) -> None:
        with self.sessions.begin() as db:
            payment = db.scalar(select(PaymentRow).where(PaymentRow.deal_id == deal_id))
            if payment is None:
                raise KeyError(deal_id)
            payment.status = "PAID"
            payment.provider_payment_id = payment_id
            deal = db.get(DealRow, deal_id)
            assert deal is not None
            deal.status = "PAID"

    def add_daily_concession_cost(self, amount: int) -> None:
        with self.sessions.begin() as db:
            row = db.get(RiskStateRow, 1)
            assert row is not None
            self._roll_over(row)
            row.daily_concession_cost += amount

    def agent_state(self) -> dict[str, Any]:
        """Authoritative agent/usage state so the UI never has to guess."""
        usage = self.get_usage()
        return {
            "agent_frozen": bool(usage["agent_frozen"]),
            "violation_count": int(usage["violation_count"]),
            "daily_concession_cost": int(usage["daily_concession_cost"]),
            "transactions_today": int(usage["transactions_today"]),
        }

    def audit(self, actor: str, action: str, decision: str | None = None, request_id: str | None = None, data: dict[str, Any] | None = None) -> None:
        with self.sessions.begin() as db:
            db.add(AuditRow(actor=actor, action=action, decision=decision, request_id=request_id, data=data or {}))

    def dashboard(self) -> dict[str, Any]:
        """Every figure here is derived from stored rows. No constants, no estimates."""
        settled = {"PAYMENT_CREATED", "PAID"}
        won = {"AUTHORIZED", "PAYMENT_CREATED", "PAID"}
        with self.sessions() as db:
            deals = db.scalars(select(DealRow)).all()
            negotiations = db.scalar(select(func.count(SessionRow.id))) or 0
            blocks = db.scalar(select(func.count(EventRow.id)).where(EventRow.level == "BLOCKED")) or 0
            risk_events = db.scalar(select(func.count(EventRow.id)).where(EventRow.level.in_(["BLOCKED", "WARNING"]))) or 0

            revenue = 0
            won_count = 0
            concession_costs: list[int] = []
            customer_values: list[int] = []
            profit_protected = 0
            for row in deals:
                price = int(row.data.get("final_price", 0))
                if row.status in settled:
                    revenue += price
                if row.status in won:
                    won_count += 1
                    policy = self._policy_snapshot(db, str(row.data.get("product_id", "")))
                    if policy is None:
                        continue
                    ids = set(row.data.get("concession_ids") or [])
                    chosen = [item for item in policy.concessions if item.id in ids]
                    concession_costs.append(sum(item.merchant_cost for item in chosen))
                    customer_values.append(sum(item.customer_perceived_value for item in chosen))
                    # Value actually defended: what the merchant kept above its own hard floor.
                    profit_protected += max(price - policy.min_acceptable_price, 0)

            def mean(values: list[int]) -> int:
                return round(sum(values) / len(values)) if values else 0

            return {
                "revenue": revenue,
                "negotiations": negotiations,
                "deals_won": won_count,
                "policy_blocks": blocks,
                "profit_protected": profit_protected,
                "average_concession_cost": mean(concession_costs),
                "average_customer_value": mean(customer_values),
                "conversion": round((won_count / negotiations) * 100, 1) if negotiations else 0.0,
                "risk_events": risk_events,
            }

    @staticmethod
    def _policy_snapshot(db: Session, product_id: str) -> MerchantPolicy | None:
        row = db.get(PolicyRow, product_id) if product_id else None
        return MerchantPolicy.model_validate(row.data) if row else None

    def daily_activity(self, days: int = 7) -> list[dict[str, Any]]:
        """Real per-day series for the dashboard charts, oldest day first."""
        today = utc_now().date()
        window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        buckets = {
            day: {"negotiations": 0, "authorized": 0, "blocked": 0, "revenue": 0, "concession": 0, "value": 0}
            for day in window
        }
        start = datetime.combine(window[0], datetime.min.time(), tzinfo=timezone.utc)
        with self.sessions() as db:
            for created_at, in db.execute(select(SessionRow.created_at).where(SessionRow.created_at >= start)):
                day = self._as_date(created_at)
                if day in buckets:
                    buckets[day]["negotiations"] += 1
            for level, created_at in db.execute(select(EventRow.level, EventRow.created_at).where(EventRow.created_at >= start)):
                day = self._as_date(created_at)
                if day in buckets and level == "BLOCKED":
                    buckets[day]["blocked"] += 1
            for row in db.scalars(select(DealRow).where(DealRow.created_at >= start)):
                day = self._as_date(row.created_at)
                if day not in buckets:
                    continue
                buckets[day]["authorized"] += 1
                if row.status in {"PAYMENT_CREATED", "PAID"}:
                    buckets[day]["revenue"] += int(row.data.get("final_price", 0))
                # Concession cost vs customer value, re-derived from the product's own policy.
                policy = self._policy_snapshot(db, str(row.data.get("product_id", "")))
                if policy is None:
                    continue
                ids = set(row.data.get("concession_ids") or [])
                chosen = [item for item in policy.concessions if item.id in ids]
                buckets[day]["concession"] += sum(item.merchant_cost for item in chosen)
                buckets[day]["value"] += sum(item.customer_perceived_value for item in chosen)
        return [
            {"day": day.strftime("%a"), "date": day.isoformat(), **buckets[day]}
            for day in window
        ]

    def block_reasons(self) -> list[dict[str, Any]]:
        """Real distribution of DealGuard block codes, highest first."""
        with self.sessions() as db:
            rows = db.execute(
                select(EventRow.decision_code, func.count(EventRow.id))
                .where(EventRow.level == "BLOCKED", EventRow.decision_code.is_not(None))
                .group_by(EventRow.decision_code)
                .order_by(desc(func.count(EventRow.id)))
            ).all()
        total = sum(count for _, count in rows)
        return [
            {
                "code": code,
                "name": str(code).replace("_", " ").title(),
                "count": count,
                "percent": round((count / total) * 100, 1) if total else 0.0,
            }
            for code, count in rows
        ]

    @staticmethod
    def _as_date(value: datetime) -> date:
        return value.date() if value.tzinfo else value.replace(tzinfo=timezone.utc).date()
