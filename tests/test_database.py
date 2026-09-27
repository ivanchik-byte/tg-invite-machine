import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.models.models import Account, TargetGroup, AudienceMember, Proxy

@pytest.mark.asyncio
async def test_database_initialization_and_models(test_session):
    test_proxy = Proxy(
        host="127.0.0.1",
        port=9050,
        protocol="socks5",
        is_active=True
    )
    test_session.add(test_proxy)
    await test_session.commit()

    test_account = Account(
        phone="+1234567890",
        session_encrypted="encrypted_token_sample",
        first_name="Ivan",
        username="ivan_test",
        status="active",
        proxy_id=test_proxy.id
    )
    test_session.add(test_account)

    test_target = TargetGroup(
        title="Portfolio Channel",
        username="portfolio_chat",
        is_active=True
    )
    test_session.add(test_target)
    await test_session.commit()

    test_member = AudienceMember(
        tg_id=987654321,
        access_hash=1234567890123456,
        username="target_user",
        source_chat="source_group",
        status="pending",
        target_group_id=test_target.id
    )
    test_session.add(test_member)
    await test_session.commit()

    fetched_account = (await test_session.execute(
        select(Account).where(Account.phone == "+1234567890")
    )).scalars().first()

    assert fetched_account is not None
    assert fetched_account.first_name == "Ivan"
    assert fetched_account.proxy_id == test_proxy.id

    fetched_member = (await test_session.execute(
        select(AudienceMember).where(AudienceMember.username == "target_user")
    )).scalars().first()

    assert fetched_member is not None
    assert fetched_member.status == "pending"
    assert fetched_member.access_hash == 1234567890123456

@pytest.mark.asyncio
async def test_account_daily_quota_rollover(test_session):
    yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    account = Account(
        phone="+9999999999",
        session_encrypted="encrypted_sample",
        daily_invites_count=20,
        last_invite_at=yesterday
    )
    test_session.add(account)
    await test_session.commit()

    assert account.get_effective_daily_invites() == 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    account.record_invite(now)
    assert account.daily_invites_count == 1
    assert account.last_invite_at == now
