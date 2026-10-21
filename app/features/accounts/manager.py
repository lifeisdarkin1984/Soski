import asyncio
import re
import time
from dataclasses import dataclass
from typing import Dict, Any, Optional
from enum import Enum
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded
from pyrogram.types import User
from sqlalchemy import select
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.crypto import crypto
from app.core.exceptions import AccountAuthError
from app.core.logger import logger
from app.models import Account, TelegramSession, AccountStatus

# اگر کارگر با این خطاها شروع نشود یعنی سشن دیگر معتبر نیست
DEAD_SESSION_ERRORS = {
    "AuthKeyUnregistered", "AuthKeyInvalid", "UserDeactivated", "UserDeactivatedBan",
    "SessionRevoked", "SessionExpired",
}
FRIENDLY_ERRORS = {
    "PhoneNumberInvalid": "شماره نامعتبر است.",
    "PhoneNumberBanned": "این شماره در تلگرام مسدود است.",
    "PhoneCodeInvalid": "کد اشتباه است.",
    "PhoneCodeExpired": "کد منقضی شده؛ دوباره از اول شروع کنید.",
    "PasswordHashInvalid": "رمز دومرحله‌ای اشتباه است.",
    "FloodWait": "تلگرام موقتاً محدود کرده؛ بعداً تلاش کنید.",
}


class WorkerStatus(Enum):
    running = "running"
    stopped = "stopped"
    error = "error"
    starting = "starting"


class WorkerManager:
    def __init__(self):
        self.workers: Dict[int, Any] = {}
        self.tasks: Dict[int, Any] = {}
        self.statuses: Dict[int, WorkerStatus] = {}

    async def start_worker(self, account_id: int, worker):
        if account_id in self.workers:
            return
        self.statuses[account_id] = WorkerStatus.starting
        self.workers[account_id] = worker
        self.tasks[account_id] = asyncio.create_task(self._run_worker(account_id, worker))

    async def _run_worker(self, account_id: int, worker):
        try:
            await worker.start()
            self.statuses[account_id] = WorkerStatus.running
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.statuses[account_id] = WorkerStatus.error
            logger.error(f"Worker {account_id} failed: {type(e).__name__}: {e}", exc_info=True)
            if type(e).__name__ in DEAD_SESSION_ERRORS:
                await AccountManager.set_status(account_id, AccountStatus.inactive)
                logger.error(f"Account {account_id} session is no longer valid; account deactivated.")
            try:
                await worker.stop()
            except Exception:
                pass
            self.workers.pop(account_id, None)

    async def stop_worker(self, account_id: int):
        task = self.tasks.pop(account_id, None)
        if task:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        worker = self.workers.pop(account_id, None)
        if worker:
            await worker.stop()
        self.statuses[account_id] = WorkerStatus.stopped

    def get_worker_status(self, account_id: int):
        return self.statuses.get(account_id)

    async def health_check(self) -> Dict[int, bool]:
        health = {}
        for account_id, worker in list(self.workers.items()):
            try:
                health[account_id] = (await worker.client.get_me()) is not None
            except Exception:
                health[account_id] = False
        return health


worker_manager = WorkerManager()


@dataclass
class PendingLogin:
    client: Client
    phone_code_hash: str
    created: float


class AccountManager:
    def __init__(self):
        self._pending: Dict[str, PendingLogin] = {}

    # ---------- ورود با شماره ----------
    @staticmethod
    def normalize_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) < 8:
            raise AccountAuthError("شماره نامعتبر است. مثال: +989123456789")
        return "+" + digits

    @staticmethod
    def _friendly(e: Exception) -> str:
        return FRIENDLY_ERRORS.get(type(e).__name__, f"{type(e).__name__}")

    async def _drop_pending(self, phone: str):
        pending = self._pending.pop(phone, None)
        if pending:
            try:
                await pending.client.disconnect()
            except Exception:
                pass

    async def begin_login(self, phone: str) -> str:
        phone = self.normalize_phone(phone)
        await self._drop_pending(phone)
        client = Client(name=f"login_{int(time.time())}", api_id=settings.api_id,
                        api_hash=settings.api_hash, in_memory=True)
        try:
            await client.connect()
            sent = await client.send_code(phone)
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise AccountAuthError(self._friendly(e))
        self._pending[phone] = PendingLogin(client, sent.phone_code_hash, time.time())
        return phone

    async def submit_code(self, phone: str, code: str) -> str:
        """'done' یا 'password_required'"""
        phone = self.normalize_phone(phone)
        pending = self._pending.get(phone)
        if not pending:
            raise AccountAuthError("درخواست ورود پیدا نشد؛ از اول شروع کنید.")
        digits = re.sub(r"\D", "", code or "")
        if not digits:
            raise AccountAuthError("کد را فقط با عدد بفرستید.")
        try:
            result = await pending.client.sign_in(phone, pending.phone_code_hash, digits)
        except SessionPasswordNeeded:
            return "password_required"
        except Exception as e:
            if type(e).__name__ != "PhoneCodeInvalid":
                await self._drop_pending(phone)
            raise AccountAuthError(self._friendly(e))
        if not isinstance(result, User):
            await self._drop_pending(phone)
            raise AccountAuthError("این شماره در تلگرام ثبت‌نام نشده است.")
        await self._finish(phone)
        return "done"

    async def submit_password(self, phone: str, password: str) -> str:
        phone = self.normalize_phone(phone)
        pending = self._pending.get(phone)
        if not pending:
            raise AccountAuthError("درخواست ورود پیدا نشد؛ از اول شروع کنید.")
        try:
            await pending.client.check_password(password)
        except Exception as e:
            if type(e).__name__ != "PasswordHashInvalid":
                await self._drop_pending(phone)
            raise AccountAuthError(self._friendly(e))
        await self._finish(phone)
        return "done"

    async def cancel_login(self, phone: Optional[str]):
        if phone:
            await self._drop_pending(self.normalize_phone(phone))

    async def _finish(self, phone: str) -> int:
        pending = self._pending[phone]
        session_string = await pending.client.export_session_string()
        encrypted = crypto.encrypt(session_string)
        async with AsyncSessionLocal() as db:
            account = (await db.execute(select(Account).where(Account.phone == phone))).scalar_one_or_none()
            if account is None:
                account = Account(phone=phone, status=AccountStatus.active)
                db.add(account)
                await db.flush()
            else:
                account.status = AccountStatus.active
            record = await db.get(TelegramSession, account.id)
            if record is None:
                db.add(TelegramSession(account_id=account.id, session_string=encrypted))
            else:
                record.session_string = encrypted
            await db.commit()
            account_id = account.id
        await self._drop_pending(phone)
        logger.info(f"Account {account_id} saved.")
        return account_id

    # ---------- مدیریت ----------
    @staticmethod
    async def set_status(account_id: int, status: AccountStatus):
        async with AsyncSessionLocal() as db:
            account = await db.get(Account, account_id)
            if account:
                account.status = status
                await db.commit()

    @staticmethod
    async def start_account(account_id: int) -> bool:
        from app.features.accounts.worker import AccountWorker  # جلوگیری از import حلقوی
        if account_id in worker_manager.workers:
            return False
        async with AsyncSessionLocal() as db:
            account = await db.get(Account, account_id)
            record = await db.get(TelegramSession, account_id)
        if not account or not record:
            logger.warning(f"No account/session for id {account_id}")
            return False
        try:
            session_string = crypto.decrypt(record.session_string)
        except Exception:
            logger.error(f"Could not decrypt session of account {account_id}")
            return False
        await AccountManager.set_status(account_id, AccountStatus.active)
        await worker_manager.start_worker(account_id, AccountWorker(account_id, account.phone, session_string))
        return True

    @staticmethod
    async def start_all_active() -> int:
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(
                select(Account.id).where(Account.status == AccountStatus.active))).scalars().all()
        started = 0
        for account_id in ids:
            if await AccountManager.start_account(account_id):
                started += 1
            await asyncio.sleep(1)
        return started

    @staticmethod
    async def stop_account(account_id: int):
        await worker_manager.stop_worker(account_id)
        await AccountManager.set_status(account_id, AccountStatus.inactive)

    @staticmethod
    async def remove_account(account_id: int):
        await worker_manager.stop_worker(account_id)
        async with AsyncSessionLocal() as db:
            account = await db.get(Account, account_id)
            if account:
                await db.delete(account)
                await db.commit()

    @staticmethod
    async def restart_all() -> int:
        for account_id in list(worker_manager.workers.keys()):
            await worker_manager.stop_worker(account_id)
        return await AccountManager.start_all_active()


account_manager = AccountManager()
