import ipaddress
import logging
from datetime import datetime
from urllib.parse import urlparse

import discord
from aiohttp import web
from discord.ext import commands

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != "config":
        raise
    from .. import config

try:
    from . import role_policy
except ModuleNotFoundError as exc:
    if exc.name not in {"role_policy", "cogs"}:
        raise
    import cogs.role_policy as role_policy

try:
    from .notify import broadcast_notification, send_relay_notification
except ModuleNotFoundError as exc:
    if exc.name not in {"notify", "cogs"}:
        raise
    from cogs.notify import broadcast_notification, send_relay_notification

try:
    from .team import send_team_notification
except ModuleNotFoundError as exc:
    if exc.name not in {"team", "cogs"}:
        raise
    from cogs.team import send_team_notification

try:
    import db
except ModuleNotFoundError as exc:
    if exc.name != "db":
        raise
    from .. import db

try:
    from db import team as db_team
except ModuleNotFoundError as exc:
    if exc.name not in {"db", "team"}:
        raise
    from ..db import team as db_team

try:
    from db import audit as db_audit
except ModuleNotFoundError as exc:
    if exc.name not in {"db", "audit"}:
        raise
    from ..db import audit as db_audit


LOGGER = logging.getLogger("ops.bot.internal_api")
BOT_API = urlparse(config.BOT_API_URL)
ALLOWED_RELAY_SHARES = {25, 50, 75, 90, 100}


class InternalApiCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @staticmethod
    def _authorized(request: web.Request) -> bool:
        token = request.headers.get("api-bot-key")
        return str(token or "").strip() == config.BOT_API_KEY

    @staticmethod
    def _parse_event_time(value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _normalize_id_list(value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = str(value).split(",")
        result: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            normalized = str(item or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    async def _apply_cguard_role_state(
        self,
        *,
        guild_id: str,
        discord_user_id: str,
        authenticated: bool,
        reason: str,
    ) -> bool:
        guild = self.bot.get_guild(int(guild_id)) if str(guild_id).isdigit() else None
        if guild is None:
            return False

        member = guild.get_member(int(discord_user_id)) if str(discord_user_id).isdigit() else None
        if member is None and str(discord_user_id).isdigit():
            try:
                member = await guild.fetch_member(int(discord_user_id))
            except discord.NotFound:
                member = None
        if member is None:
            return False

        await role_policy.apply_auth_state(member, authenticated=authenticated, reason=reason)
        return True

    async def cog_load(self) -> None:
        app = web.Application()
        app.router.add_post("/api/v1/run/requests/notify/{channel_id}", self._handle_notify)
        app.router.add_post("/api/v1/run/requests/relay/{channel_id}", self._handle_relay)
        app.router.add_post("/api/v1/run/requests/notify/team", self._handle_team_notification)
        app.router.add_post("/api/v1/run/requests/cguard/ai", self._handle_cguard_ai)
        app.router.add_post("/api/v1/run/requests/cguard/onoff", self._handle_cguard_onoff)
        app.router.add_post("/api/v1/run/requests/team/role", self._handle_team_role_create)
        app.router.add_post("/api/v1/run/requests/team/role/assign", self._handle_team_role_assign)
        app.router.add_delete("/api/v1/run/requests/team/role/{role_id}", self._handle_team_role_delete)
        app.router.add_post("/api/v1/run/requests/operator/role", self._handle_operator_role_grant)
        app.router.add_delete("/api/v1/run/requests/operator/role/{discord_user_id}", self._handle_operator_role_revoke)
        app.router.add_get("/api/v1/run/requests/guild/members", self._handle_guild_members)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        host = BOT_API.hostname or "127.0.0.1"
        port = BOT_API.port or 5000
        self._site = web.TCPSite(self._runner, host=host, port=port)
        try:
            await self._site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            winerror = getattr(exc, "winerror", None)
            err_no = getattr(exc, "errno", None)
            if winerror == 10048 or err_no == 10048:
                LOGGER.warning(
                    "Internal bot API disabled because %s:%s is already in use (WinError 10048).",
                    host,
                    port,
                )
                return
            raise
        LOGGER.info("Internal bot API listening on %s:%s", host, port)

    async def cog_unload(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _handle_health(self, request: web.Request) -> web.Response:
        """운영포털 대시보드 헬스체크 — 인증 불필요, 봇 생존 신호만 반환."""
        return web.json_response({"status": "ok", "service": "discord_bot"})

    async def _handle_notify(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        channel_id = str(request.match_info.get("channel_id", "")).strip() or None
        title = str(data.get("title", data.get("category", ""))).strip()
        description = str(data.get("description", "")).strip()
        requested_by = str(data.get("requested_by", "portal")).strip() or "portal"

        if not title or not description:
            return web.json_response({"error": "title and description are required"}, status=400)

        try:
            sent_count = await broadcast_notification(
                self.bot,
                title,
                description,
                requested_by=requested_by,
                target_channel_id=channel_id,
            )
        except Exception as exc:
            LOGGER.exception("Failed to broadcast internal notification")
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response(
            {
                "status": "sent",
                "title": title,
                "channel_id": channel_id,
                "sent_count": sent_count,
            }
        )

    @staticmethod
    def _relay_message_for_share(title: str, description: str, share: int | None) -> str:
        if share is None:
            return description
        if share == 100:
            return f'🏁 공격팀 "{title}" 팀이 승리했습니다!'
        templates = {
            25: "⚠️ 앗….. {title}팀이 25퍼 점령했어요….!!!",
            50: "🚨 앗…… {title}팀이 50퍼 점령……. 이러다 망할지도???",
            75: "🔥 앗….. 사실상 {title}팀이 이긴 거 같은데요 75퍼!!!!!!!",
            90: "👑 {title}팀이 머지 않았어요!!!! 90퍼!!!!!!!!!!!에요",
        }
        template = templates.get(share)
        return template.format(title=title) if template else description

    async def _handle_relay(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "validation_failed", "fields": ["invalid_json"]}, status=400)

        channel_id = str(request.match_info.get("channel_id", "")).strip()
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        requested_by = str(data.get("requested_by", "relay")).strip() or "relay"
        match_id = str(data.get("match_id", "")).strip() or None
        team_id = str(data.get("team_id", "")).strip() or None

        share_value = data.get("share")
        share: int | None = None
        if share_value is not None and str(share_value).strip() != "":
            try:
                share = int(share_value)
            except (TypeError, ValueError):
                return web.json_response({"error": "share must be an integer"}, status=400)
            if share not in ALLOWED_RELAY_SHARES:
                return web.json_response({"error": "share must be one of 25,50,75,90,100"}, status=400)

        if not title or not description:
            return web.json_response({"error": "title and description are required"}, status=400)

        event_type = "share" if share is not None else "relay"
        created_at = db._now_kst_naive()
        relay_text = self._relay_message_for_share(title, description, share)
        event_key = (
            f"{match_id}:{team_id}:{share}"
            if (match_id and team_id and share is not None)
            else None
        )
        payload = {
            "title": title,
            "description": description,
            "share": share,
            "match_id": match_id,
            "team_id": team_id,
            "channel_id": channel_id,
            "event_type": event_type,
            "requested_by": requested_by,
        }

        try:
            await send_relay_notification(
                self.bot,
                channel_id=channel_id,
                title=title,
                relay_text=relay_text,
                requested_by=requested_by,
                share=share,
            )
            db_audit.create_log(
                log_type="relay",
                action="RELAY_DISPATCH",
                event_key=event_key,
                actor_name=requested_by,
                channel_id=channel_id,
                status="sent",
                detail=f"relay sent to channel {channel_id}",
                payload=payload,
                timestamp=created_at,
            )
        except Exception as exc:
            LOGGER.exception("Failed to relay message channel_id=%s", channel_id)
            try:
                db_audit.create_log(
                    log_type="relay",
                    action="RELAY_DISPATCH",
                    event_key=event_key,
                    actor_name=requested_by,
                    channel_id=channel_id,
                    status="failed",
                    detail=str(exc),
                    payload=payload,
                    timestamp=created_at,
                )
            except Exception:
                LOGGER.exception("Failed to write relay audit log channel_id=%s", channel_id)
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response(
            {
                "status": "accepted",
                "channel_id": channel_id,
                "event_type": event_type,
                "share": share,
                "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
            },
            status=200,
        )

    async def _handle_team_notification(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        data = await request.json()
        guild_id = str(data.get("guild_id", config.GUILD_ID)).strip()
        team_ids = self._normalize_id_list(data.get("team_ids"))
        role_ids = self._normalize_id_list(data.get("role_ids"))
        legacy_team_id = str(data.get("team_id", "")).strip()
        legacy_role_id = str(data.get("role_id", data.get("discord_role_id", ""))).strip()
        if legacy_team_id:
            team_ids.append(legacy_team_id)
        if legacy_role_id:
            role_ids.append(legacy_role_id)
        team_ids = self._normalize_id_list(team_ids)
        role_ids = self._normalize_id_list(role_ids)
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        severity = str(data.get("severity", "")).strip() or None
        event_key = str(data.get("event_key", "")).strip()
        channel_id = str(data.get("channel_id", "")).strip() or None

        if not title or not description:
            return web.json_response({"error": "title and description are required"}, status=400)
        if not event_key:
            return web.json_response({"error": "event_key is required"}, status=400)
        if not team_ids and not role_ids:
            return web.json_response({"error": "team_id/team_ids or role_id/role_ids is required"}, status=400)

        targets: list[dict[str, str | None | dict]] = []
        unresolved: list[dict[str, str | None]] = []
        seen_roles: set[str] = set()

        for role_id in role_ids:
            team_payload = db_team.get_team_by_role(guild_id, role_id)
            team_id = str((team_payload or {}).get("id") or "").strip() or None
            if role_id in seen_roles:
                continue
            seen_roles.add(role_id)
            targets.append({"team_id": team_id, "role_id": role_id, "team_payload": team_payload})

        for team_id in team_ids:
            team_payload = db_team.get_team_role_mapping(guild_id, team_id)
            role_id = str((team_payload or {}).get("discord_role_id") or "").strip()
            normalized_team_id = str((team_payload or {}).get("id") or team_id).strip() or None
            if not role_id:
                unresolved.append({"team_id": normalized_team_id, "error": "role mapping not found"})
                continue
            if role_id in seen_roles:
                continue
            seen_roles.add(role_id)
            targets.append({"team_id": normalized_team_id, "role_id": role_id, "team_payload": team_payload})

        if not targets and unresolved:
            return web.json_response(
                {
                    "status": "failed",
                    "event_key": event_key,
                    "errors": unresolved,
                },
                status=404,
            )

        total_targets = len(targets)
        sent_count = 0
        duplicate_count = 0
        failed_count = 0
        results: list[dict[str, str | int | None]] = []

        for target in targets:
            role_id = str(target.get("role_id") or "").strip()
            team_id = str(target.get("team_id") or "").strip() or None
            target_event_key = event_key if total_targets == 1 else f"{event_key}:{role_id}"
            created, event_row = db_team.create_team_notification_event(
                event_key=target_event_key,
                guild_id=guild_id,
                team_id=team_id,
                discord_role_id=role_id,
                title=title,
                description=description,
                severity=severity,
                status="accepted",
            )
            if not created:
                duplicate_count += 1
                results.append(
                    {
                        "status": "duplicate",
                        "event_key": target_event_key,
                        "team_id": str(event_row.get("team_id") or "") or None,
                        "role_id": str(event_row.get("discord_role_id") or "") or None,
                    }
                )
                continue
            try:
                await send_team_notification(
                    self.bot,
                    guild_id=guild_id,
                    role_id=role_id,
                    title=title,
                    description=description,
                    channel_id=channel_id,
                    severity=severity,
                    team_payload=target.get("team_payload") if isinstance(target.get("team_payload"), dict) else None,
                )
                db_team.update_team_notification_event(target_event_key, status="sent")
                sent_count += 1
                results.append(
                    {
                        "status": "accepted",
                        "event_key": target_event_key,
                        "team_id": team_id,
                        "role_id": role_id,
                    }
                )
            except Exception as exc:
                LOGGER.exception("Failed to send team notification event_key=%s", target_event_key)
                db_team.update_team_notification_event(target_event_key, status="failed", error_message=str(exc))
                failed_count += 1
                results.append(
                    {
                        "status": "failed",
                        "event_key": target_event_key,
                        "team_id": team_id,
                        "role_id": role_id,
                        "error": str(exc),
                    }
                )

        for item in unresolved:
            failed_count += 1
            results.append(
                {
                    "status": "failed",
                    "event_key": event_key,
                    "team_id": item.get("team_id"),
                    "role_id": None,
                    "error": item.get("error") or "role mapping not found",
                }
            )

        overall_status = "accepted"
        http_status = 200
        if sent_count == 0 and failed_count > 0 and duplicate_count == 0:
            overall_status = "failed"
            http_status = 404
        elif failed_count > 0:
            overall_status = "partial"
            http_status = 207

        return web.json_response(
            {
                "status": overall_status,
                "event_key": event_key,
                "requested_team_ids": team_ids,
                "requested_role_ids": role_ids,
                "sent_count": sent_count,
                "duplicate_count": duplicate_count,
                "failed_count": failed_count,
                "results": results,
            },
            status=http_status,
        )

    async def _handle_cguard_ai(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "validation_failed", "fields": ["invalid_json"]}, status=400)

        ip = str(data.get("ip", "")).strip()
        nickname = str(data.get("nickname", "")).strip()
        time_raw = str(data.get("time", "")).strip()
        discord_user_id = str(data.get("discord_user_id", "")).strip() or None
        guild_id = str(data.get("guild_id", config.GUILD_ID)).strip()
        fields: list[str] = []

        try:
            ipaddress.ip_address(ip)
        except Exception:
            fields.append("ip")
        if not nickname:
            fields.append("nickname")
        try:
            event_time = self._parse_event_time(time_raw)
        except Exception:
            fields.append("time")
            event_time = None
        if discord_user_id is not None and not discord_user_id.isdigit():
            fields.append("discord_user_id")

        if fields:
            return web.json_response({"error": "validation_failed", "fields": fields}, status=400)

        try:
            db.create_cguard_log(
                ip=ip,
                nickname=nickname,
                event_time=event_time,
                discord_user_id=discord_user_id,
            )
            if discord_user_id:
                db.set_cguard_user_status(
                    discord_user_id,
                    "blocked",
                    reason="C-GUARD AI detection",
                    source="bot.run.requests.cguard.ai",
                    event_time=event_time,
                )
                await self._apply_cguard_role_state(
                    guild_id=guild_id,
                    discord_user_id=discord_user_id,
                    authenticated=False,
                    reason="C-GUARD AI detection",
                )
            # Event queue has been decommissioned; keep equivalent event emission in logs.
            LOGGER.info("cguard.status_changed user=%s status=blocked", discord_user_id or "-")
            LOGGER.info("cguard.alert ip=%s nickname=%s", ip, nickname)
        except Exception:
            LOGGER.exception("Failed to handle C-GUARD AI request")
            return web.json_response({"error": "internal_error"}, status=500)

        return web.json_response(
            {
                "status": "accepted",
                "cguard_status": "blocked" if discord_user_id else None,
                "discord_user_id": discord_user_id,
                "ip": ip,
                "nickname": nickname,
                "time": time_raw,
            },
            status=201,
        )

    async def _handle_cguard_onoff(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "validation_failed", "fields": ["invalid_json"]}, status=400)

        discord_user_id = str(data.get("discord_user_id", "")).strip()
        nickname = str(data.get("nickname", "")).strip()
        onoff = str(data.get("onoff", "")).strip().lower()
        time_raw = str(data.get("time", "")).strip()
        ip = str(data.get("ip", "-")).strip() or "-"
        guild_id = str(data.get("guild_id", config.GUILD_ID)).strip()
        fields: list[str] = []

        if not discord_user_id or not discord_user_id.isdigit():
            fields.append("discord_user_id")
        if not nickname:
            fields.append("nickname")
        if onoff not in {"on", "off"}:
            fields.append("onoff")
        try:
            event_time = self._parse_event_time(time_raw)
        except Exception:
            fields.append("time")
            event_time = None
        if ip != "-":
            try:
                ipaddress.ip_address(ip)
            except Exception:
                fields.append("ip")

        if fields:
            return web.json_response({"error": "validation_failed", "fields": fields}, status=400)

        normalized_status = "enabled" if onoff == "on" else "blocked"
        try:
            db.create_cguard_onoff_log(
                discord_user_id=discord_user_id,
                nickname=nickname,
                onoff=onoff,
                event_time=event_time,
                ip=ip,
            )
            db.set_cguard_user_status(
                discord_user_id,
                normalized_status,
                reason="C-GUARD onoff update",
                source="bot.run.requests.cguard.onoff",
                event_time=event_time,
            )
            await self._apply_cguard_role_state(
                guild_id=guild_id,
                discord_user_id=discord_user_id,
                authenticated=(normalized_status == "enabled"),
                reason="C-GUARD onoff update",
            )
            LOGGER.info("cguard.status_changed user=%s status=%s", discord_user_id, normalized_status)
            LOGGER.info("cguard.alert user=%s onoff=%s", discord_user_id, onoff)
        except Exception:
            LOGGER.exception("Failed to handle C-GUARD onoff request")
            return web.json_response({"error": "internal_error"}, status=500)

        return web.json_response(
            {
                "status": "accepted",
                "cguard_status": normalized_status,
                "discord_user_id": discord_user_id,
                "ip": ip,
                "nickname": nickname,
                "time": time_raw,
            },
            status=201,
        )

    async def _handle_team_role_create(self, request: web.Request) -> web.Response:
        """운영포털 팀 생성 시 호출 — 같은 이름 역할이 있으면 재사용, 없으면 신규 생성."""
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "validation_failed", "fields": ["invalid_json"]}, status=400)

        team_name = str(data.get("team_name", "")).strip()
        if not team_name:
            return web.json_response({"error": "validation_failed", "fields": ["team_name"]}, status=400)

        guild = self.bot.get_guild(int(config.GUILD_ID)) if str(config.GUILD_ID).isdigit() else None
        if guild is None:
            LOGGER.warning("team role create: guild not found guild_id=%s", config.GUILD_ID)
            return web.json_response({"error": "guild_not_found"}, status=404)

        # 같은 이름 역할이 이미 있으면 그것을 재사용 (중복 생성 방지)
        existing = discord.utils.get(guild.roles, name=team_name)
        if existing is not None:
            LOGGER.info("team role reused name=%s role_id=%s", team_name, existing.id)
            return web.json_response(
                {
                    "role_id": str(existing.id),
                    "role_name": existing.name,
                    "created": False,
                },
                status=200,
            )

        try:
            role = await guild.create_role(
                name=team_name,
                reason=f"운영포털 팀 생성: {team_name}",
                mentionable=True,
            )
        except discord.Forbidden:
            LOGGER.warning("team role create: missing permissions name=%s", team_name)
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception as exc:
            LOGGER.exception("Failed to create team role name=%s", team_name)
            return web.json_response({"error": str(exc)}, status=500)

        LOGGER.info("team role created name=%s role_id=%s", team_name, role.id)
        return web.json_response(
            {
                "role_id": str(role.id),
                "role_name": role.name,
                "created": True,
            },
            status=201,
        )

    async def _handle_team_role_delete(self, request: web.Request) -> web.Response:
        """운영포털 팀 삭제 시 호출 — 이미 없는 역할이면 deleted=false로 200 응답 (404 아님)."""
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        role_id_raw = str(request.match_info.get("role_id", "")).strip()
        if not role_id_raw or not role_id_raw.isdigit():
            return web.json_response({"error": "validation_failed", "fields": ["role_id"]}, status=400)

        guild = self.bot.get_guild(int(config.GUILD_ID)) if str(config.GUILD_ID).isdigit() else None
        if guild is None:
            LOGGER.warning("team role delete: guild not found guild_id=%s", config.GUILD_ID)
            return web.json_response({"error": "guild_not_found"}, status=404)

        role = guild.get_role(int(role_id_raw))
        if role is None:
            # 이미 수동 삭제되었거나 다른 길드 역할 — 멱등 처리로 200 반환
            LOGGER.info("team role delete: already gone role_id=%s", role_id_raw)
            return web.json_response(
                {"deleted": False, "reason": "not_found"},
                status=200,
            )

        try:
            await role.delete(reason="운영포털 팀 삭제")
        except discord.Forbidden:
            LOGGER.warning("team role delete: missing permissions role_id=%s", role_id_raw)
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception as exc:
            LOGGER.exception("Failed to delete team role role_id=%s", role_id_raw)
            return web.json_response({"error": str(exc)}, status=500)

        LOGGER.info("team role deleted role_id=%s", role_id_raw)
        return web.json_response({"deleted": True}, status=200)

    async def _handle_team_role_assign(self, request: web.Request) -> web.Response:
        """운영포털 팀원 추가 시 호출 — 대상 멤버에게 팀 역할을 부여한다."""
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "validation_failed", "fields": ["invalid_json"]}, status=400)

        discord_user_id = str(data.get("discord_user_id", "")).strip()
        role_id_raw = str(data.get("role_id", "")).strip()
        if not discord_user_id or not discord_user_id.isdigit():
            return web.json_response({"error": "validation_failed", "fields": ["discord_user_id"]}, status=400)
        if not role_id_raw or not role_id_raw.isdigit():
            return web.json_response({"error": "validation_failed", "fields": ["role_id"]}, status=400)

        guild = self.bot.get_guild(int(config.GUILD_ID)) if str(config.GUILD_ID).isdigit() else None
        if guild is None:
            LOGGER.warning("team role assign: guild not found guild_id=%s", config.GUILD_ID)
            return web.json_response({"error": "guild_not_found"}, status=404)

        member = guild.get_member(int(discord_user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(discord_user_id))
            except discord.NotFound:
                LOGGER.info("team role assign: member not found user_id=%s", discord_user_id)
                return web.json_response({"error": "member_not_found"}, status=404)
            except discord.Forbidden:
                LOGGER.warning("team role assign: forbidden to fetch member user_id=%s", discord_user_id)
                return web.json_response({"error": "forbidden"}, status=403)
            except Exception as exc:
                LOGGER.exception("Failed to fetch member user_id=%s", discord_user_id)
                return web.json_response({"error": str(exc)}, status=500)

        role = guild.get_role(int(role_id_raw))
        if role is None:
            LOGGER.info("team role assign: role not found role_id=%s", role_id_raw)
            return web.json_response({"error": "role_not_found"}, status=404)

        if role in member.roles:
            return web.json_response(
                {
                    "user_id": discord_user_id,
                    "role_id": role_id_raw,
                    "granted": False,
                    "reason": "already_has_role",
                },
                status=200,
            )

        try:
            await member.add_roles(role, reason="운영포털 팀원 추가")
        except discord.Forbidden:
            LOGGER.warning("team role assign: missing permissions user_id=%s role_id=%s", discord_user_id, role_id_raw)
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception as exc:
            LOGGER.exception("Failed to assign team role user_id=%s role_id=%s", discord_user_id, role_id_raw)
            return web.json_response({"error": str(exc)}, status=500)

        LOGGER.info("team role assigned user_id=%s role_id=%s", discord_user_id, role_id_raw)
        return web.json_response(
            {"user_id": discord_user_id, "role_id": role_id_raw, "granted": True},
            status=200,
        )

    async def _handle_guild_members(self, request: web.Request) -> web.Response:
        """운영포털 동기화용 — 길드 멤버 전체 목록을 반환한다."""
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        guild = self.bot.get_guild(int(config.GUILD_ID)) if str(config.GUILD_ID).isdigit() else None
        if guild is None:
            LOGGER.warning("guild members: guild not found guild_id=%s", config.GUILD_ID)
            return web.json_response({"error": "guild_not_found"}, status=404)

        try:
            if not guild.chunked:
                await guild.chunk(cache=True)
        except Exception:
            LOGGER.exception("Failed to chunk guild members guild_id=%s", guild.id)

        members = sorted(
            guild.members,
            key=lambda member: (member.bot, member.display_name.lower()),
        )
        payload = [
            {
                "discord_user_id": str(member.id),
                "username": member.name,
                "display_name": member.display_name,
                "global_name": member.global_name,
                "nick": member.nick,
                "is_bot": member.bot,
            }
            for member in members
        ]
        return web.json_response(
            {
                "guild_id": str(guild.id),
                "total": len(payload),
                "members": payload,
            },
            status=200,
        )

    async def _handle_operator_role_grant(self, request: web.Request) -> web.Response:
        """운영포털 운영자 추가 시 호출 — 해당 Discord 사용자에게 '운영자' 역할 부여 (멱등)."""
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "validation_failed", "fields": ["invalid_json"]}, status=400)

        discord_user_id = str(data.get("discord_user_id", "")).strip()
        if not discord_user_id or not discord_user_id.isdigit():
            return web.json_response({"error": "validation_failed", "fields": ["discord_user_id"]}, status=400)

        guild = self.bot.get_guild(int(config.GUILD_ID)) if str(config.GUILD_ID).isdigit() else None
        if guild is None:
            LOGGER.warning("operator role grant: guild not found guild_id=%s", config.GUILD_ID)
            return web.json_response({"error": "guild_not_found"}, status=404)

        member = guild.get_member(int(discord_user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(discord_user_id))
            except discord.NotFound:
                LOGGER.info("operator role grant: member not found user_id=%s", discord_user_id)
                return web.json_response({"error": "member_not_found"}, status=404)
            except discord.Forbidden:
                LOGGER.warning("operator role grant: forbidden to fetch member user_id=%s", discord_user_id)
                return web.json_response({"error": "forbidden"}, status=403)
            except Exception as exc:
                LOGGER.exception("Failed to fetch member user_id=%s", discord_user_id)
                return web.json_response({"error": str(exc)}, status=500)

        try:
            role = await role_policy.ensure_operator_role(guild)
        except discord.Forbidden:
            LOGGER.warning("operator role grant: missing permissions to ensure operator role")
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception as exc:
            LOGGER.exception("Failed to ensure operator role")
            return web.json_response({"error": str(exc)}, status=500)

        if role in member.roles:
            LOGGER.info("operator role grant: already has role user_id=%s role_id=%s", discord_user_id, role.id)
            return web.json_response(
                {
                    "user_id": discord_user_id,
                    "role_id": str(role.id),
                    "granted": False,
                    "reason": "already_has_role",
                },
                status=200,
            )

        try:
            await member.add_roles(role, reason="운영포털 운영자 지정")
        except discord.Forbidden:
            LOGGER.warning("operator role grant: missing permissions to add role user_id=%s", discord_user_id)
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception as exc:
            LOGGER.exception("Failed to grant operator role user_id=%s", discord_user_id)
            return web.json_response({"error": str(exc)}, status=500)

        LOGGER.info("operator role granted user_id=%s role_id=%s", discord_user_id, role.id)
        return web.json_response(
            {
                "user_id": discord_user_id,
                "role_id": str(role.id),
                "granted": True,
            },
            status=200,
        )

    async def _handle_operator_role_revoke(self, request: web.Request) -> web.Response:
        """운영포털 운영자 비활성화 시 호출 — 해당 Discord 사용자의 '운영자' 역할 제거 (멱등)."""
        if not self._authorized(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        discord_user_id = str(request.match_info.get("discord_user_id", "")).strip()
        if not discord_user_id or not discord_user_id.isdigit():
            return web.json_response({"error": "validation_failed", "fields": ["discord_user_id"]}, status=400)

        guild = self.bot.get_guild(int(config.GUILD_ID)) if str(config.GUILD_ID).isdigit() else None
        if guild is None:
            LOGGER.warning("operator role revoke: guild not found guild_id=%s", config.GUILD_ID)
            return web.json_response({"error": "guild_not_found"}, status=404)

        member = guild.get_member(int(discord_user_id))
        if member is None:
            try:
                member = await guild.fetch_member(int(discord_user_id))
            except discord.NotFound:
                LOGGER.info("operator role revoke: member not found user_id=%s", discord_user_id)
                return web.json_response(
                    {"revoked": False, "reason": "member_not_found"},
                    status=200,
                )
            except Exception as exc:
                LOGGER.exception("Failed to fetch member user_id=%s", discord_user_id)
                return web.json_response({"error": str(exc)}, status=500)

        role = role_policy.operator_role(guild)
        if role is None or role not in member.roles:
            LOGGER.info("operator role revoke: no role to remove user_id=%s", discord_user_id)
            return web.json_response(
                {"revoked": False, "reason": "no_role"},
                status=200,
            )

        try:
            await member.remove_roles(role, reason="운영포털 운영자 비활성화")
        except discord.Forbidden:
            LOGGER.warning("operator role revoke: missing permissions user_id=%s", discord_user_id)
            return web.json_response({"error": "forbidden"}, status=403)
        except Exception as exc:
            LOGGER.exception("Failed to revoke operator role user_id=%s", discord_user_id)
            return web.json_response({"error": str(exc)}, status=500)

        LOGGER.info("operator role revoked user_id=%s role_id=%s", discord_user_id, role.id)
        return web.json_response(
            {"revoked": True, "role_id": str(role.id)},
            status=200,
        )


async def setup(bot):
    await bot.add_cog(InternalApiCog(bot))
