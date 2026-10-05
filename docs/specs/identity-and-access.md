# Identity and access specification

| | |
|---|---|
| Area prefix | `IAM` (used in rule identifiers, for example `IAM-ROLE-2`) |
| Verified at | commit `61bebe96` (`v0.6.0-76-g61bebe96`), 2026-10-05 |
| Owning services | auth (`services/auth/`), acl (`services/acl/`), the shared `herd_common` auth, internal-token, and ACL helpers (`services/common/herd_common/`) |
| Other services involved | every service that verifies a JWT (all but config), notifications (admin and contact lookups), inventory (group lookups, ACL and reservation-owner checks), secrets (ACL checks), reservations (batch group lookup, the reservation-owner answer), an LDAP or Active Directory server |
| Design records | [ADR 0011](../design/0011-ldap-group-sync.md) |
| Related guides | [ROLES.md](../ROLES.md), [ARCHITECTURE.md](../ARCHITECTURE.md) (Authentication, Machine-token exchange), [ADMIN_HANDBOOK.md](../ADMIN_HANDBOOK.md), [ENV_VARS.md](../ENV_VARS.md), [SECURITY.md](../../SECURITY.md) |

All API paths below are each service's own paths. Through the gateway the auth
service's paths are prefixed with `/api/auth` and the acl service's with `/api/acl`
(for example `POST /api/auth/login`).

## 1. Purpose

This area answers two questions for every request HERD receives: who is calling, and
what may they do. The auth service holds user accounts, signs people in with a local
password or a directory (LDAP) password, issues the short-lived access tokens every
other service trusts, holds user groups, mirrors directory groups into them, and issues
long-lived API tokens for machines. The acl service holds per-resource grants that let a
user group view or manage one device, topology, reservation, or secret. This area
specifies the mechanisms other areas use to authorize; it does not specify each other
service's per-route rule.

## 2. Actors and permissions

The endpoint matrix is in [ROLES.md](../ROLES.md). The rules that go beyond role are
numbered in section 8.

| Actor | May | May not |
|---|---|---|
| Unauthenticated caller | Register (local mode only), log in, refresh, log out, exchange an API token, read `/health` and `/version` | Anything else; every other route answers 401 |
| User | Read their own account, list groups by name, read any user's group memberships, ask the acl service about their own access | Read a group's members, change any group, list users, change roles or activation, create API tokens, manage grants, ask about another user's access |
| Admin | Everything a user may; list users; activate any account; deactivate any account other than their own and the superadmin's; manage groups and members; manage directory mappings and run directory sync; create API tokens for any principal of their rank or lower; list and revoke every API token; manage grants; ask about any user's access | Change any role; assign the superadmin role; deactivate the superadmin or themselves; mint a token above their own rank |
| Superadmin | Everything an admin may; set any other non-superadmin account's role to user or admin; mint superadmin tokens | Change their own role or another superadmin's; assign the superadmin role; deactivate themselves |
| Another service (internal token) | List active admin ids, read a user's contact details, read an active user's groups (auth); run a permission check for a user id (acl) | Anything through the user-facing routes |

## 3. Concepts and data

| Concept | Meaning | Owner | Stored in |
|---|---|---|---|
| User account | Email, username, role, `auth_source` (`local` or `ldap`), `is_active`, `deactivated_by_sync`, a bcrypt hash for local accounts only | auth | `users` (`User` in `services/auth/app/models/user.py`) |
| Role | `user`, `admin`, or `superadmin`, ranked in that order | auth | `users.role` |
| Access token | A signed JWT carrying `sub`, `username`, `email`, `role`, `exp`, `iat` (and `auth_source` when minted from an API token). Never stored | auth (issues); every service verifies | the client |
| Refresh token | An opaque random value; only its SHA-256 hash is stored, with an expiry and a revoked flag | auth | `refresh_tokens` (`RefreshToken`) |
| API token | A long-lived machine credential for a principal account, with its own role and optional expiry; only its SHA-256 hash is stored | auth | `api_tokens` (`ApiToken` in `services/auth/app/models/api_token.py`) |
| User group | A named team of users | auth | `user_groups` (`UserGroup` in `services/auth/app/models/group.py`) |
| Group membership | One user in one group | auth | `group_members` (`GroupMember`) |
| "Not Grouped" | An ordinary group, found by that exact name, that holds users who belong to no other group | auth | `user_groups` |
| Directory mapping | One directory group DN mirrored into one HERD group | auth | `ldap_group_mappings` (`LdapGroupMapping` in `services/auth/app/models/ldap_group_mapping.py`) |
| Sync run | The audit row of one directory sync pass: trigger, status, counters, capped detail | auth | `ldap_sync_runs` (`LdapSyncRun` in `services/auth/app/models/ldap_sync_run.py`) |
| Resource grant | A user group's `view` or `manage` permission on one resource. `group_id` is a bare auth id and `resource_id` a bare id of another service's resource, neither with a foreign key | acl | `resource_grants` (`ResourceGrant` in `services/acl/app/models/grant.py`) |
| Internal token | The shared secret `INTERNAL_API_TOKEN` sent as `X-Internal-Token` on service-to-service calls with no acting user | every service | environment |

## 4. State model

Four things in this area have a lifecycle: an account's active flag, a refresh token, an
API token, and a sync run.

**Statuses.**

- Account `active`: `is_active` true; the account can sign in.
- Account `inactive (manual)`: `is_active` false and `deactivated_by_sync` false. Only an
  admin brings it back.
- Account `inactive (sync)`: `is_active` false and `deactivated_by_sync` true. The
  directory sync may bring it back.
- Refresh token `live`: not revoked. It stops working when `expires_at` passes; expiry
  writes nothing.
- Refresh token `revoked`: consumed by a refresh or revoked by a logout. Terminal.
- API token `active` and `revoked` (`is_active` false, terminal). An optional
  `expires_at` stops an active token without a write.
- Sync run `running`, then one of `success`, `partial`, `aborted`, `failed`. The four
  outcomes are terminal.

**Transitions.**

| From | To | Performed by | Guard | Stages | Rule |
|---|---|---|---|---|---|
| (none) | account `active` | `POST /register`; LDAP login; directory sync provisioning; startup seed | see the feature rules | nothing | IAM-ACCT-6 |
| account `active` | account `inactive (manual)` | `POST /users/{id}/deactivate` | admin; not self; not the superadmin | nothing | IAM-ACCT-1 |
| any account status | account `active` | `POST /users/{id}/activate` | admin | nothing | IAM-ACCT-2 |
| account `active` | account `inactive (sync)` | directory sync deactivation sweep | proven absent or disabled; breaker not tripped; row still active at write time | nothing | IAM-ACCT-3 |
| account `inactive (sync)` | account `active` | directory sync deactivation sweep | proven present and not disabled; row still inactive with sync provenance at write time | nothing | IAM-ACCT-4 |
| (none) | refresh token `live` | `POST /login`, `POST /refresh` | credentials valid | nothing | IAM-SESSION-1 |
| refresh token `live` | refresh token `revoked` | `POST /refresh` | not revoked, not expired, owner active, all at write time | nothing | IAM-SESSION-2 |
| refresh token `live` | refresh token `revoked` | `POST /logout` | none | nothing | IAM-SESSION-3 |
| (none) | API token `active` | `POST /tokens` | admin; rank checks | nothing | IAM-APITOK-1, IAM-APITOK-2 |
| API token `active` | API token `revoked` | `DELETE /tokens/{id}` | admin | nothing | IAM-APITOK-7 |
| (none) | run `running` | sync-now, interval tick, direct `run_sync` | run slot acquired | nothing | IAM-RUN-1 |
| run `running` | `success`, `partial`, `aborted`, `failed` | the run's own finalizer | see rules | nothing | IAM-RUN-2, IAM-RUN-3, IAM-RUN-4, IAM-RUN-5 |
| run `running` | run `failed` | stale-run reaper | older than the stale threshold, still `running` at write time | nothing | IAM-RUN-6 |

The deactivation sweep and the manual endpoints are the only writers of `is_active`;
nothing deletes a user account.

**Concurrency.** The sweep's flips and the reaper are conditional `UPDATE ... WHERE`
compare-and-swaps, so a concurrent admin write or a finalizing run wins (IAM-ACCT-3,
IAM-ACCT-4, IAM-RUN-6). Refresh consumes its token with a conditional update that
re-checks every liveness condition (IAM-SESSION-2). Manual activate and deactivate are
plain ORM writes; only one sync run can execute at a time (IAM-SERIAL-1).

**Rules.**

- **IAM-ACCT-1.** Deactivate is refused 409 when the target is the caller (checked
  before the target is looked up), 404 for an unknown id, and 400 when the target holds
  the superadmin role; otherwise it sets `is_active` false and `deactivated_by_sync`
  false. An admin may deactivate another admin. \
  Enforced in: `services/auth/app/routers/admin.py` (`deactivate_user`); `services/auth/app/services/auth_service.py` (`set_user_active`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_cannot_deactivate_own_account`, `test_deactivate_nonexistent_user_404`, `test_admin_cannot_deactivate_superadmin`, `test_superadmin_cannot_deactivate_another_superadmin`, `test_admin_can_deactivate_user`, `test_deactivate_and_activate_clear_sync_provenance`)
- **IAM-ACCT-2.** Activate answers 404 for an unknown id and otherwise sets `is_active`
  true and `deactivated_by_sync` false, on any target including the superadmin and an
  account that is already active. The missing superadmin carve-out is by decision
  (docstring of `deactivate_user`; [ROLES.md](../ROLES.md), Admin Management): it is the
  recovery path. \
  Enforced in: `services/auth/app/routers/admin.py` (`activate_user`); `services/auth/app/services/auth_service.py` (`set_user_active`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_activate_nonexistent_user_404`, `test_activate_superadmin_has_no_carve_out`, `test_superadmin_can_deactivate_and_reactivate_user`); `services/auth/tests/test_routers_direct_ldap_admin.py` (`test_activate_user_direct_success_clears_sync_provenance`)
- **IAM-ACCT-3.** A sweep deactivation is `UPDATE users SET is_active = false,
  deactivated_by_sync = true WHERE id = :id AND is_active`; a row an admin changed after
  the sweep read it is left alone. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_apply_sweep_flips`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_guarded_update_yields_to_concurrent_admin_write`, `test_sweep_counters_and_detail_apply_to_run_row`)
- **IAM-ACCT-4.** A sweep reactivation is `UPDATE users SET is_active = true,
  deactivated_by_sync = false WHERE id = :id AND NOT is_active AND deactivated_by_sync`,
  so an account an admin deactivated is never reactivated by the directory. By decision;
  see ADR 0011, Reactivation. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_apply_sweep_flips`, `_run_deactivation_sweep`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_provenance_gate`, `test_sweep_guarded_update_yields_to_concurrent_admin_write`)
- **IAM-ACCT-5.** An inactive account cannot log in (local or LDAP), refresh, exchange
  an API token for itself, or pass any JWT-guarded auth route (401), and auth's internal
  group lookup answers 404 for it. Other services keep accepting an access token it
  already holds until that token expires (IAM-CLAIM-2). \
  Enforced in: `services/auth/app/dependencies/auth.py` (`get_current_user`); `services/auth/app/services/auth_service.py` (`_authenticate_local`, `_authenticate_ldap`, `rotate_refresh_token`); `services/auth/app/services/api_token_service.py` (`exchange_api_token`); `services/auth/app/routers/internal.py` (`get_user_groups_internal`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_inactive_user_blocked_from_me_and_refresh`); `services/auth/tests/test_auth_service_unit.py` (`test_authenticate_user_rejects_deactivated_local_user`); `services/auth/tests/test_auth_ldap.py` (`test_ldap_login_inactive_user_returns_401`); `services/auth/tests/test_api_tokens.py` (`test_exchange_inactive_principal_returns_none`); `services/auth/tests/test_internal.py` (`test_internal_user_groups_404_for_inactive_user`)
- **IAM-ACCT-6.** Every account is created active: by registration (local, role user),
  by LDAP login or directory sync provisioning (LDAP, role user, no password hash), or
  by the startup seed (local, role superadmin). \
  Enforced in: `services/auth/app/services/auth_service.py` (`create_user`, `create_ldap_user`); `services/auth/app/models/user.py` (`User`) \
  Pinned by: `services/auth/tests/test_routers_direct.py` (`test_register_endpoint_success`); `services/auth/tests/test_ldap_sync_service.py` (`test_preprovision_creates_ldap_user_without_password`); `services/auth/tests/test_main.py` (`test_seed_superadmin_creates_new_superadmin`)
- **IAM-SESSION-1.** Login and refresh each issue one new refresh token: a random UUID4
  of which only the SHA-256 hash is stored, expiring after
  `AUTH_REFRESH_TOKEN_EXPIRE_DAYS` (default 7). \
  Enforced in: `services/auth/app/utils/jwt.py` (`create_refresh_token`, `hash_token`); `services/auth/app/services/auth_service.py` (`create_tokens_for_user`) \
  Pinned by: none
- **IAM-SESSION-2.** Refresh consumes the presented token with one conditional update
  that requires it to be unrevoked, unexpired, and owned by an active account at write
  time; the loser of a race with a logout, another refresh, an expiry, or a deactivation
  issues nothing and answers 401. \
  Enforced in: `services/auth/app/services/auth_service.py` (`rotate_refresh_token`) \
  Pinned by: `services/auth/tests/test_auth_service_unit.py` (`test_concurrent_logout_during_refresh_does_not_resurrect_session`, `test_concurrent_refresh_single_winner`, `test_token_expiring_during_refresh_does_not_mint`, `test_user_deactivated_during_refresh_does_not_mint`); `services/auth/tests/test_auth.py` (`test_refresh_with_already_used_token`, `test_expired_refresh_token_rejected`)
- **IAM-SESSION-3.** Logout revokes the presented refresh token if it exists and always
  answers 204, even for an unknown token; it does not touch the access token, which
  stays valid until it expires. \
  Enforced in: `services/auth/app/routers/auth.py` (`logout`); `services/auth/app/services/auth_service.py` (`revoke_refresh_token`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_logout_revokes_refresh_token`, `test_logout_with_invalid_token`, `test_full_auth_lifecycle`)
- **IAM-RUN-1.** A sync run row is committed with status `running` before any directory
  work starts, so a process that dies mid-run leaves a visible row. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`create_run`, `run_sync`, `start_background_run`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_running_row_is_committed_before_directory_work`, `test_insert_run_applies_server_defaults`)
- **IAM-RUN-2.** A run that completes ends `success` when no degrading detail was
  recorded and `partial` otherwise; every detail category degrades except
  `provision_races`, `deactivated`, and `reactivated`. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`execute_run`, `_Tally`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_set_difference_applies_exact_diff_through_group_service`, `test_member_skip_reasons_counted_without_failing_group`, `test_tally_apply_to_writes_deactivation_counters_and_stays_non_degrading`)
- **IAM-RUN-3.** A tripped circuit breaker (IAM-SWEEP-7) makes the run `aborted` with
  the breaker message as `error`, overriding `success` and `partial`. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`execute_run`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_breaker_both_terms_exceeded_aborts_but_still_reactivates`, `test_run_status_vocabulary_includes_aborted`)
- **IAM-RUN-4.** An exception in the run machinery rolls back the uncommitted work and
  ends the run `failed` with the exception text as `error`; the exception is not
  re-raised. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`execute_run`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_machinery_exception_yields_failed_run_with_error`)
- **IAM-RUN-5.** A cancellation (service shutdown) rolls back, ends the run `failed`
  with error `cancelled during service shutdown`, and re-raises the cancellation, also
  when the finalizing commit itself fails. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`execute_run`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_execute_run_cancelled_mid_reconcile_commits_failed_with_cancelled_error`, `test_execute_run_cancelled_with_broken_finalize_commit_still_raises_cancelled`)
- **IAM-RUN-6.** The reaper sets every row still `running` whose `started_at` is older
  than the stale threshold (IAM-REAP-2) to `failed` with error `run did not finalize
  (process died mid-run)` and a `finished_at`, by one conditional update, so a run that
  finalized first keeps its outcome. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`reap_stale_running_runs`, `STALE_RUN_ERROR`) \
  Pinned by: `services/auth/tests/test_ldap_sync_stale_run_reaper.py` (`test_stale_running_row_flips_to_failed_with_exact_error_and_finished_at`, `test_fresh_running_row_younger_than_threshold_is_untouched`, `test_terminal_rows_untouched_regardless_of_age`, `test_cas_leaves_a_row_a_racing_finalize_claimed_between_cutoff_and_update`)

## 5. API surface

Auth service:

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| POST | `/register` | anyone, unauthenticated; local mode only | 201 | IAM-REG-1 to IAM-REG-6 |
| POST | `/login` | anyone, unauthenticated | 200 | IAM-LOGIN-1 to IAM-LOGIN-4, IAM-LDAP-1 to IAM-LDAP-11 |
| POST | `/refresh` | anyone holding a refresh token | 200 | IAM-SESSION-1, IAM-SESSION-2, IAM-JWT-2 |
| POST | `/logout` | anyone, unauthenticated | 204 | IAM-SESSION-3 |
| GET | `/me` | any signed-in active account | 200 | IAM-JWT-3, IAM-ROLE-8 |
| GET | `/users` | admin, superadmin | 200 | IAM-USER-1 |
| PUT | `/users/{id}/role` | superadmin | 200 | IAM-USER-2 to IAM-USER-7 |
| POST | `/users/{id}/activate` | admin, superadmin | 200 | IAM-ACCT-2 |
| POST | `/users/{id}/deactivate` | admin, superadmin | 200 | IAM-ACCT-1 |
| POST | `/tokens` | admin, superadmin | 201 | IAM-APITOK-1 to IAM-APITOK-5 |
| GET | `/tokens` | admin, superadmin | 200 | IAM-APITOK-6 |
| DELETE | `/tokens/{id}` | admin, superadmin | 204 | IAM-APITOK-7 |
| POST | `/tokens/exchange` | anyone, unauthenticated | 200 | IAM-APITOK-8, IAM-APITOK-9, IAM-APITOK-10 |
| GET | `/groups` | any signed-in active account | 200 | IAM-GROUP-1 |
| POST | `/groups` | admin, superadmin | 201 | IAM-GROUP-3 |
| GET | `/groups/{id}` | admin, superadmin | 200 | IAM-GROUP-2 |
| PUT | `/groups/{id}` | admin, superadmin | 200 | IAM-GROUP-3, IAM-GROUP-4 |
| DELETE | `/groups/{id}` | admin, superadmin | 204 | IAM-GROUP-5 |
| POST | `/groups/{id}/members` | admin, superadmin | 201 | IAM-GROUP-6 |
| DELETE | `/groups/{id}/members/{user_id}` | admin, superadmin | 204 | IAM-GROUP-7 |
| POST | `/groups/{id}/members/bulk` | admin, superadmin | 200 | IAM-GROUP-8, IAM-GROUP-13 |
| POST | `/groups/{id}/members/bulk-remove` | admin, superadmin | 200 | IAM-GROUP-9 |
| GET | `/groups/user/{user_id}` | any signed-in active account, for any user id | 200 | IAM-GROUP-10 |
| POST | `/groups/users/groups` | any signed-in active account, for any user ids | 200 | IAM-GROUP-10 |
| GET | `/admin/ldap-sync/status` | admin, superadmin; any auth mode | 200 | IAM-LOOP-7 |
| POST | `/admin/ldap-sync/mappings` | admin, superadmin; LDAP mode only | 201 | IAM-MAP-1 to IAM-MAP-7 |
| GET | `/admin/ldap-sync/mappings` | admin, superadmin; any auth mode | 200 | IAM-MAP-8 |
| DELETE | `/admin/ldap-sync/mappings/{id}` | admin, superadmin; any auth mode | 204 | IAM-MAP-8 |
| POST | `/admin/ldap-sync/run` | admin, superadmin; LDAP mode only | 202 | IAM-SERIAL-1, IAM-SERIAL-2 |
| GET | `/admin/ldap-sync/runs` | admin, superadmin | 200 | IAM-LOOP-8 |
| GET | `/admin/ldap-sync/runs/{id}` | admin, superadmin | 200 | IAM-LOOP-8 |

Every route marked admin or superadmin checks the effective role (IAM-ROLE-5), answers
401 to an unauthenticated caller or an inactive account and 403 to a lower role, and
checks the role before it looks anything up (IAM-ROLE-7). List routes take `skip`
(default 0) and `limit` (1 to 500, default 50) and answer `{items, total, skip, limit}`.

ACL service:

| Method | Path | Who may call | Success | Rules |
|---|---|---|---|---|
| POST | `/grants` | admin, superadmin (role claim) | 201 | IAM-ACL-1 to IAM-ACL-5 |
| GET | `/grants` | admin, superadmin (role claim) | 200 | IAM-ACL-4, IAM-ACL-6 |
| GET | `/grants/{id}` | admin, superadmin (role claim) | 200 | IAM-ACL-4 |
| DELETE | `/grants/{id}` | admin, superadmin (role claim) | 204 | IAM-ACL-4, IAM-ACL-13 |
| POST | `/check` | any signed-in caller, about themselves; admin about anyone | 200 | IAM-ACL-7 to IAM-ACL-9 |
| POST | `/check/batch` | any signed-in caller, about themselves; admin about anyone | 200 | IAM-ACL-8, IAM-ACL-9, IAM-ACL-10 |
| GET | `/resources` | any signed-in caller, about themselves; admin about anyone | 200 | IAM-ACL-8, IAM-ACL-9, IAM-ACL-11 |

Both services also serve unauthenticated `GET /health` and `GET /version`
(`operations-and-observability.md`).

## 6. Events

None. Neither service publishes or consumes a NATS event.

## 7. Internal API

| Method | Path | Auth | Caller | Answers | Rules |
|---|---|---|---|---|---|
| GET | `/internal/admins` (auth) | `X-Internal-Token` | notifications (health fan-out) | list of user ids | IAM-INTERNAL-2, IAM-INTERNAL-3 |
| GET | `/internal/users/{user_id}/contact` (auth) | `X-Internal-Token` | notifications (outbound channels) | `{user_id, email, username}` | IAM-INTERNAL-2, IAM-INTERNAL-4 |
| GET | `/internal/users/{user_id}/groups` (auth) | `X-Internal-Token` | acl (`POST /internal/check`) | list of groups, same shape as `GET /groups/user/{user_id}` | IAM-INTERNAL-2, IAM-INTERNAL-5 |
| POST | `/internal/check` (acl) | `X-Internal-Token` | inventory (scheduled apply re-check, through `herd_common`) | `{allowed, grants}` | IAM-INTERNAL-2, IAM-ACL-12 |

Two user-facing auth routes are also called service to service with a forwarded user
JWT: `GET /groups/user/{user_id}` by acl and inventory, and `POST /groups/users/groups`
by reservations reporting (IAM-GROUP-10).

## 8. Features

### 8.1 Registration

**What it does.** On a deployment that uses local passwords, anyone can create an
account with an email, a username, and a password, then sign in with it.

**Surfaces.** User interface `frontend/src/pages/RegisterPage.tsx`; route
`POST /register` (body `RegisterRequest` in `services/auth/app/schemas/auth.py`).

**Rules.**

- **IAM-REG-1.** When `AUTH_METHOD=ldap`, registration is refused with 409 before the
  body is used. \
  Enforced in: `services/auth/app/routers/auth.py` (`register`) \
  Pinned by: `services/auth/tests/test_auth_ldap.py` (`test_register_blocked_when_ldap_mode`)
- **IAM-REG-2.** The email must be a valid address; the username 3 to 32 characters of
  letters, digits, `_`, and `-`; the password 8 to 72 characters (72 is bcrypt's input
  limit). \
  Enforced in: `services/auth/app/schemas/auth.py` (`RegisterRequest`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_register_invalid_email`, `test_register_username_too_short`, `test_register_username_invalid_chars`, `test_register_username_max_length`, `test_register_password_too_short`, `test_register_password_too_long`)
- **IAM-REG-3.** An email or username that already exists, found by the pre-check or by
  a concurrent insert, answers one generic 409 `Email or username already exists`. \
  Enforced in: `services/auth/app/routers/auth.py` (`register`) \
  Pinned by: `services/auth/tests/test_routers_direct.py` (`test_register_endpoint_duplicate_email`, `test_register_endpoint_duplicate_username`, `test_register_endpoint_integrity_error_fallback`)
- **IAM-REG-4.** A registered account is local, role `user`, active, with a bcrypt hash
  of the password; the response is the account and carries no token. \
  Enforced in: `services/auth/app/services/auth_service.py` (`create_user`, `get_password_hash`) \
  Pinned by: `services/auth/tests/test_routers_direct.py` (`test_register_endpoint_success`); `services/auth/tests/test_auth_service_unit.py` (`test_get_password_hash_produces_verifiable_hash`)
- **IAM-REG-5.** A new account (registered or LDAP-provisioned) is added to "Not
  Grouped" when a group of that exact name exists; a failure there is rolled back and
  logged and never fails the account creation. \
  Enforced in: `services/auth/app/services/auth_service.py` (`_auto_assign_not_grouped`) \
  Pinned by: `services/auth/tests/test_auth_service_unit.py` (`test_create_user_auto_assigns_to_not_grouped`, `test_create_user_handles_not_grouped_failure`); `services/auth/tests/test_auth_ldap.py` (`test_ldap_jit_provision_auto_assigns_to_not_grouped`)
- **IAM-REG-6.** No email verification exists: an account is usable as soon as it is
  created. \
  Enforced in: `services/auth/app/routers/auth.py` (`register`) \
  Pinned by: `tests/e2e/test_register_and_roles.py` (`test_registered_user_can_login`)

**Out of scope.** Password reset, password change, and account deletion do not exist.

### 8.2 Local login

**What it does.** A user signs in with their email and password and receives a
short-lived access token and a refresh token.

**Surfaces.** User interface `frontend/src/pages/LoginPage.tsx`; route `POST /login`
(body `LoginRequest`).

**Rules.**

- **IAM-LOGIN-1.** In local mode the account is found by exact email; an unknown email,
  an LDAP-sourced account, a wrong password, and an inactive account all answer the same
  401 `Invalid credentials`. \
  Enforced in: `services/auth/app/services/auth_service.py` (`_authenticate_local`, `authenticate_user`); `services/auth/app/routers/auth.py` (`login`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_login_wrong_password`, `test_login_nonexistent_email`); `services/auth/tests/test_auth_ldap.py` (`test_local_mode_blocks_ldap_user`); `services/auth/tests/test_auth_service_unit.py` (`test_authenticate_user_rejects_deactivated_local_user`)
- **IAM-LOGIN-2.** For an unknown email or an LDAP-sourced account, bcrypt still runs
  against a fixed dummy hash, so response time does not reveal whether the email exists. \
  Enforced in: `services/auth/app/services/auth_service.py` (`_authenticate_local`, `_DUMMY_HASH`) \
  Pinned by: none
- **IAM-LOGIN-3.** The login identifier is 1 to 255 characters and the password at most
  72. \
  Enforced in: `services/auth/app/schemas/auth.py` (`LoginRequest`) \
  Pinned by: none
- **IAM-LOGIN-4.** A successful login answers `{access_token, refresh_token,
  token_type: "bearer"}`. \
  Enforced in: `services/auth/app/routers/auth.py` (`login`); `services/auth/app/schemas/auth.py` (`TokenResponse`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_login`)

**Out of scope.** Rate limiting and lockout after failed attempts do not exist.

### 8.3 LDAP login and just-in-time provisioning

**What it does.** On a deployment set to `AUTH_METHOD=ldap`, a user signs in with their
directory login name and password. The first successful sign-in creates their HERD
account automatically.

**Surfaces.** Route `POST /login`; directory client `services/auth/app/services/ldap_service.py`.

**Rules.**

- **IAM-LDAP-1.** The mode is global: with `AUTH_METHOD=ldap` every login goes to the
  directory, so a local account, including the seeded superadmin, cannot log in. \
  Enforced in: `services/auth/app/services/auth_service.py` (`authenticate_user`) \
  Pinned by: `services/auth/tests/test_auth_ldap.py` (`test_ldap_mode_rejects_existing_local_account`)
- **IAM-LDAP-2.** The login `email` field is the directory login name; it is
  filter-escaped and substituted into `LDAP_USER_FILTER`. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_search_user`) \
  Pinned by: `services/auth/tests/test_ldap_service.py` (`test_filter_escaping_via_ldap3_conv`); `services/auth/tests/test_ldap_service_live.py` (`test_bind_user_filter_metacharacters_are_escaped`)
- **IAM-LDAP-3.** Login binds twice: first as the service account (anonymously when
  `LDAP_BIND_DN` is empty) to search `LDAP_USER_BASE_DN` and take the first match, then
  as that entry's DN with the submitted password. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_bind_user_sync`, `_search_user`, `_bind_as_user`) \
  Pinned by: `services/auth/tests/test_ldap_service.py` (`test_bind_user_success`, `test_bind_user_wrong_password`); `services/auth/tests/test_ldap_service_live.py` (`test_bind_user_success_returns_identity`)
- **IAM-LDAP-4.** An empty password is refused before any directory call (an LDAP
  simple bind with an empty password would succeed anonymously). \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_bind_user_sync`) \
  Pinned by: `services/auth/tests/test_ldap_service.py` (`test_bind_user_empty_password_rejected`); `services/auth/tests/test_ldap_service_live.py` (`test_bind_user_empty_password_rejected`)
- **IAM-LDAP-5.** Login fails closed with 401: incomplete configuration, a directory
  that cannot be reached or bound, a search with no match, a refused user bind, an
  entry without the email attribute, and any LDAP error all refuse. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_bind_user_sync`, `_search_user`) \
  Pinned by: `services/auth/tests/test_ldap_service.py` (`test_bind_user_not_found`, `test_bind_user_missing_email_rejected`, `test_bind_user_missing_config_returns_none`); `services/auth/tests/test_ldap_service_live.py` (`test_bad_service_account_returns_none`, `test_bind_user_unknown_user_returns_none`)
- **IAM-LDAP-6.** Every directory connection uses StartTLS before binding unless the
  URL is `ldaps://` or `LDAP_USE_TLS` is false, and validates the server certificate
  unless `LDAP_TLS_VALIDATE` is false (which logs a warning); `LDAP_CA_CERT` names a CA
  bundle. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_build_tls`, `_build_server`, `_open_service_connection`, `_bind_as_user`) \
  Pinned by: `services/auth/tests/test_ldap_service.py` (`test_search_user_starts_tls_before_bind`, `test_bind_as_user_starts_tls_before_bind`, `test_no_start_tls_for_ldaps_url`, `test_no_start_tls_when_disabled`, `test_build_tls_validates_certificate_by_default`, `test_build_tls_uses_ca_cert_when_set`, `test_build_tls_can_opt_out_of_validation`)
- **IAM-LDAP-7.** The HERD account is found by the directory entry's email; when none
  exists one is created (`auth_source` ldap, no password hash, role user, active). \
  Enforced in: `services/auth/app/services/auth_service.py` (`_authenticate_ldap`, `create_ldap_user`) \
  Pinned by: `services/auth/tests/test_auth_ldap.py` (`test_ldap_login_jit_provisions_user`); `tests/integration/test_ldap_auth.py` (`test_ldap_login_provisions_ldap_user`)
- **IAM-LDAP-8.** An existing LDAP account is reused as stored; login never updates its
  username or role. \
  Enforced in: `services/auth/app/services/auth_service.py` (`_authenticate_ldap`) \
  Pinned by: `services/auth/tests/test_auth_ldap.py` (`test_ldap_login_reuses_existing_ldap_user`)
- **IAM-LDAP-9.** A directory email that belongs to a local account answers 401; the
  local account is never converted. \
  Enforced in: `services/auth/app/services/auth_service.py` (`_authenticate_ldap`) \
  Pinned by: `services/auth/tests/test_auth_ldap.py` (`test_ldap_mode_rejects_existing_local_account`); `services/auth/tests/test_ldap_service_live.py` (`test_authenticate_user_rejects_local_account_with_same_email`)
- **IAM-LDAP-10.** When provisioning collides with an existing username, login answers
  401 and creates nothing; it neither retries nor renames. By decision: the comment in
  `_authenticate_ldap` records that login fails closed where the directory sync repairs
  (IAM-SYNC-9, IAM-SYNC-12). \
  Enforced in: `services/auth/app/services/auth_service.py` (`_authenticate_ldap`) \
  Pinned by: `services/auth/tests/test_auth_ldap.py` (`test_ldap_login_username_collision_returns_401`)
- **IAM-LDAP-11.** The provisioned username is the entry's `LDAP_USERNAME_ATTRIBUTE`
  value, or the typed login name when the entry has none. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_bind_user_sync`) \
  Pinned by: none

**Out of scope.** Roles never come from the directory (ADR 0011, Blast radius). SAML and
OIDC do not exist (issue #37).

### 8.4 Access tokens

**What it does.** Every signed-in request carries a short-lived access token that each
service checks on its own; the browser renews it silently with the refresh token.

**Surfaces.** Routes `POST /login`, `POST /refresh`, `GET /me`; the signer in
`services/auth/app/utils/jwt.py`.

**Rules.**

- **IAM-JWT-1.** An access token is signed with `AUTH_SECRET_KEY` using
  `AUTH_ALGORITHM` (default HS256), carries `sub` (account id), `username`, `email`,
  `role`, `iat`, and `exp`, and lives `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` (default 30). \
  Enforced in: `services/auth/app/utils/jwt.py` (`create_access_token`); `services/auth/app/services/auth_service.py` (`create_tokens_for_user`) \
  Pinned by: none
- **IAM-JWT-2.** The `role` claim is the account's database role at the moment of issue,
  at login and again at every refresh, so a role change reaches a session at its next
  refresh. \
  Enforced in: `services/auth/app/services/auth_service.py` (`create_tokens_for_user`, `rotate_refresh_token`) \
  Pinned by: none
- **IAM-JWT-3.** Inside the auth service a request is authenticated only by a bearer
  token that verifies, whose `sub` is a UUID naming an existing active account;
  anything else answers 401 `Could not validate credentials`, and a missing
  Authorization header answers 401. \
  Enforced in: `services/auth/app/dependencies/auth.py` (`get_current_user`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_me_without_token`, `test_me_with_invalid_token`, `test_me_with_non_uuid_subject_returns_401`, `test_me_with_empty_bearer`, `test_me_without_bearer_prefix`); `services/auth/tests/test_routers_direct.py` (`test_get_current_user_nonexistent_user`, `test_get_current_user_inactive_user`, `test_get_current_user_no_sub_in_token`)

**Out of scope.** Token revocation lists do not exist; an access token is valid until
`exp`.

### 8.5 Roles and the effective role

**What it does.** Every account has one of three roles. Inside the auth service a
request is held to the lower of the role its token claims and the role the account has
now, so a demotion takes effect there at once.

**Surfaces.** The dependencies in `services/auth/app/dependencies/auth.py`; frontend
predicate `frontend/src/lib/roles.ts`.

**Rules.**

- **IAM-ROLE-1.** Roles rank `user` below `admin` below `superadmin`. \
  Enforced in: `services/auth/app/dependencies/auth.py` (`_ROLE_RANK`); `services/auth/app/services/api_token_service.py` (`_ROLE_RANK`, `_role_exceeds`) \
  Pinned by: `services/auth/tests/test_effective_role_unit.py` (`test_effective_role_is_the_lower_of_claim_and_db_role`)
- **IAM-ROLE-2.** The effective role is the lower of the token's `role` claim and the
  account's current database role; a missing, empty, or unknown claim counts as `user`. \
  Enforced in: `services/auth/app/dependencies/auth.py` (`effective_role`) \
  Pinned by: `services/auth/tests/test_effective_role_unit.py` (`test_claim_never_exceeds_db_role`, `test_db_role_never_exceeds_claim`, `test_missing_claim_counts_as_user`, `test_empty_claim_counts_as_user`, `test_unknown_claim_counts_as_user`, `test_matching_claim_and_db_role_is_a_no_op`)
- **IAM-ROLE-3.** The claim is read from the request's own Authorization header with
  the same parsing the bearer scheme uses (scheme compared case-insensitively). \
  Enforced in: `services/auth/app/dependencies/auth.py` (`_decode_role_claim`) \
  Pinned by: `services/auth/tests/test_effective_role_unit.py` (`test_decode_role_claim_valid_token_returns_the_claim`, `test_decode_role_claim_lowercase_scheme_matches_titlecase`); `services/auth/tests/test_effective_role_http.py` (`test_bearer_scheme_casing_does_not_change_the_outcome`)
- **IAM-ROLE-4.** With no Authorization header at all (reachable only when a test
  replaces `get_current_user`) the database role is used as is; a header that is present
  but not a decodable bearer token (wrong scheme, empty, bad signature, expired) fails
  closed to `user`. \
  Enforced in: `services/auth/app/dependencies/auth.py` (`get_effective_role`, `_ClaimAvailability`) \
  Pinned by: `services/auth/tests/test_effective_role_unit.py` (`test_get_effective_role_no_header_passes_db_role_through`, `test_get_effective_role_undecodable_fails_closed_to_user_even_for_superadmin`, `test_decode_role_claim_wrong_scheme_is_undecodable`, `test_decode_role_claim_bearer_with_empty_token_is_undecodable`, `test_decode_role_claim_garbage_token_is_undecodable`, `test_decode_role_claim_expired_token_is_undecodable`)
- **IAM-ROLE-5.** Every role gate in the auth service compares the effective role and
  answers 403 `You do not have permission to perform this action` below the required
  role. \
  Enforced in: `services/auth/app/dependencies/auth.py` (`require_role`) \
  Pinned by: `services/auth/tests/test_effective_role_http.py` (`test_user_claim_on_superadmin_row_is_refused_everywhere`, `test_admin_claim_on_superadmin_row_caps_at_admin`, `test_superadmin_claim_on_superadmin_row_is_unchanged`, `test_no_role_claim_on_admin_row_is_treated_as_user`)
- **IAM-ROLE-6.** A role gate still requires an active account, whatever the claim. \
  Enforced in: `services/auth/app/dependencies/auth.py` (`require_role`, `get_current_user`) \
  Pinned by: `services/auth/tests/test_effective_role_http.py` (`test_inactive_account_is_refused_regardless_of_claim`)
- **IAM-ROLE-7.** A role gate runs before the route looks anything up, so a lower role
  learns nothing about whether an id exists. \
  Enforced in: `services/auth/app/routers/groups.py` (`get_group`); `services/auth/app/routers/admin.py` (`update_user_role`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_get_group_not_found_user_forbidden`); `services/auth/tests/test_auth.py` (`test_admin_cannot_change_role`)
- **IAM-ROLE-8.** `GET /me` reports the account's stored role and active flag, not the
  effective role. \
  Enforced in: `services/auth/app/routers/auth.py` (`me`) \
  Pinned by: none

**Out of scope.** Ownership rules of other areas (reservations, topologies) are in
their own specifications.

### 8.6 Role trust in other services

**What it does.** Every service other than auth and config checks a caller's access
token on its own, with the shared signing key, and trusts the role the token claims.

**Surfaces.** `make_auth_dependencies` and `caller_id` in
`services/common/herd_common/auth.py`, used by acl and every other JWT-checking service.

**Rules.**

- **IAM-CLAIM-1.** A token is accepted when its signature and `exp` verify with the
  shared key and algorithm and its `sub` is non-empty; otherwise 401 `Could not validate
  credentials`. No database is consulted. \
  Enforced in: `services/common/herd_common/auth.py` (`make_auth_dependencies`) \
  Pinned by: `services/common/tests/test_auth.py` (`test_valid_token_returns_payload`, `test_expired_token_raises_401`, `test_invalid_signature_raises_401`, `test_missing_sub_raises_401`, `test_empty_sub_raises_401`, `test_malformed_token_raises_401`)
- **IAM-CLAIM-2.** The admin gate accepts a `role` claim of `admin` or `superadmin` and
  answers 403 `Admin or superadmin role required` otherwise. A demotion or deactivation
  therefore binds in these services only when the token expires. By decision; see
  [SECURITY.md](../../SECURITY.md) (Threat model, JWT) and [ROLES.md](../ROLES.md). \
  Enforced in: `services/common/herd_common/auth.py` (`make_auth_dependencies`, `ADMIN_ROLES`) \
  Pinned by: `services/common/tests/test_auth.py` (`test_require_admin_with_admin_role`, `test_require_admin_with_superadmin_role`, `test_require_admin_with_user_role_raises_403`)
- **IAM-CLAIM-3.** `caller_id` turns `sub` into a UUID and answers 401 `Invalid subject
  in token` when it is missing, not a string, or not a UUID. \
  Enforced in: `services/common/herd_common/auth.py` (`caller_id`) \
  Pinned by: `services/common/tests/test_auth.py` (`test_caller_id_parses_valid_uuid_subject`, `test_caller_id_rejects_missing_subject`, `test_caller_id_rejects_non_uuid_subject`, `test_caller_id_rejects_non_string_subject`)

**Out of scope.** How each service uses the payload (ownership, visibility) is in that
service's area.

### 8.7 API tokens

**What it does.** An admin creates a long-lived token for a machine account. The
machine trades it for a short-lived access token whenever it needs one, and an admin
can revoke it.

**Surfaces.** Routes `POST /tokens`, `GET /tokens`, `DELETE /tokens/{id}`,
`POST /tokens/exchange` (`services/auth/app/routers/tokens.py`). There is no user
interface.

**Rules.**

- **IAM-APITOK-1.** Creating a token needs an admin or superadmin effective role and a
  principal account that exists (404 `Principal user not found`); the principal may be
  any account, active or not. \
  Enforced in: `services/auth/app/routers/tokens.py` (`create_token`) \
  Pinned by: `services/auth/tests/test_api_tokens.py` (`test_create_unknown_principal_404`, `test_non_admin_cannot_create`, `test_admin_create_returns_raw_token_once`)
- **IAM-APITOK-2.** Creation is refused 403 when the requested role or the principal's
  role outranks the caller's effective role; an equal rank is allowed, so an admin may
  mint an admin token for another admin. By decision; see issue #312 and the comment in
  `create_token`. \
  Enforced in: `services/auth/app/routers/tokens.py` (`create_token`); `services/auth/app/services/api_token_service.py` (`_role_exceeds`) \
  Pinned by: `services/auth/tests/test_api_tokens.py` (`test_admin_cannot_mint_superadmin_token`, `test_admin_cannot_mint_for_superadmin_principal_even_at_admin_role`, `test_admin_cannot_request_superadmin_role`, `test_superadmin_can_mint_superadmin_token`, `test_admin_can_mint_admin_token_for_admin_principal`)
- **IAM-APITOK-3.** A token's role may not exceed its principal's role: 400 `Token role
  cannot exceed the principal's role`. \
  Enforced in: `services/auth/app/services/api_token_service.py` (`create_api_token`) \
  Pinned by: `services/auth/tests/test_api_tokens.py` (`test_role_cannot_exceed_principal_role`, `test_role_equal_to_principal_role_allowed`, `test_create_role_exceeds_principal_400`)
- **IAM-APITOK-4.** The raw token (`secrets.token_urlsafe(32)`) is returned once, in the
  create response; only its SHA-256 hash is stored and no later route returns it. \
  Enforced in: `services/auth/app/services/api_token_service.py` (`create_api_token`); `services/auth/app/schemas/api_token.py` (`ApiTokenResponse`) \
  Pinned by: `services/auth/tests/test_api_tokens.py` (`test_create_stores_only_hash_returns_raw_once`, `test_admin_create_returns_raw_token_once`)
- **IAM-APITOK-5.** `name` is 1 to 100 characters and `expires_at` is optional (no
  expiry when absent). \
  Enforced in: `services/auth/app/schemas/api_token.py` (`CreateApiTokenRequest`) \
  Pinned by: none
- **IAM-APITOK-6.** Listing returns every token, newest first, as metadata (no hash, no
  raw value), to any admin or superadmin, including tokens of a superadmin principal. \
  Enforced in: `services/auth/app/routers/tokens.py` (`list_tokens`); `services/auth/app/services/api_token_service.py` (`list_api_tokens`) \
  Pinned by: `services/auth/tests/test_routers_direct_ldap_admin.py` (`test_list_tokens_direct_returns_metadata`); `services/auth/tests/test_api_tokens.py` (`test_non_admin_cannot_list`)
- **IAM-APITOK-7.** Revoking sets `is_active` false and answers 204, also for an unknown
  or already revoked id; any admin may revoke any token. \
  Enforced in: `services/auth/app/routers/tokens.py` (`delete_token`); `services/auth/app/services/api_token_service.py` (`revoke_api_token`) \
  Pinned by: `services/auth/tests/test_api_tokens.py` (`test_revoke_is_idempotent`, `test_admin_delete_revokes`, `test_non_admin_cannot_delete`)
- **IAM-APITOK-8.** The exchange needs no authentication and answers one generic 401
  `Invalid or expired token` for an unknown, revoked, or expired token (`expires_at` at
  or before now) and for a missing or inactive principal. \
  Enforced in: `services/auth/app/services/api_token_service.py` (`exchange_api_token`); `services/auth/app/routers/tokens.py` (`exchange_token`) \
  Pinned by: `services/auth/tests/test_api_tokens.py` (`test_exchange_unknown_token_returns_none`, `test_exchange_revoked_token_returns_none`, `test_exchange_expired_token_returns_none`, `test_exchange_inactive_principal_returns_none`, `test_exchange_needs_no_auth`, `test_exchange_bad_token_generic_401`)
- **IAM-APITOK-9.** A successful exchange answers an access token whose `sub` is the
  principal, whose `role` is the lower of the token's role and the principal's current
  role, and which carries `auth_source: api_token`; no refresh token is issued, and
  `expires_in` is the access token lifetime in seconds. \
  Enforced in: `services/auth/app/services/api_token_service.py` (`exchange_api_token`); `services/auth/app/routers/tokens.py` (`exchange_token`) \
  Pinned by: `services/auth/tests/test_api_tokens.py` (`test_exchange_valid_token_mints_jwt`, `test_exchange_clamps_role_when_principal_demoted`, `test_exchange_keeps_lower_token_role_below_principal`, `test_exchange_role_unchanged_when_principal_role_unchanged`)
- **IAM-APITOK-10.** A successful exchange stamps `last_used_at`. \
  Enforced in: `services/auth/app/services/api_token_service.py` (`exchange_api_token`) \
  Pinned by: `services/auth/tests/test_api_tokens.py` (`test_exchange_sets_last_used_at`)
- **IAM-APITOK-11.** A clamped token is held to its lower role inside auth: it cannot
  pass a higher role gate or mint a token above that role. \
  Enforced in: `services/auth/app/dependencies/auth.py` (`get_effective_role`); `services/auth/app/routers/tokens.py` (`create_token`) \
  Pinned by: `services/auth/tests/test_effective_role_http.py` (`test_api_token_exchange_clamp_binds_end_to_end`)

**Out of scope.** The `/api/v1` facade's use of exchanged tokens is in `integration.md`.

### 8.8 User administration

**What it does.** Admins see every account; the superadmin promotes users to admin and
demotes them back. Activation is in the State model (IAM-ACCT-1, IAM-ACCT-2).

**Surfaces.** User interface `frontend/src/pages/admin/UsersPage.tsx` with
`frontend/src/components/admin/UserManagementTable.tsx`; routes `GET /users`,
`PUT /users/{id}/role`.

**Rules.**

- **IAM-USER-1.** Admins and superadmins list every account, active or not, ordered by
  creation time. \
  Enforced in: `services/auth/app/routers/admin.py` (`list_users`); `services/auth/app/services/auth_service.py` (`get_all_users`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_admin_can_list_users`, `test_superadmin_can_list_users`, `test_non_superadmin_gets_403`); `services/auth/tests/test_routers_direct.py` (`test_list_users_pagination`)
- **IAM-USER-2.** Only a superadmin effective role may change a role. \
  Enforced in: `services/auth/app/routers/admin.py` (`update_user_role`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_admin_cannot_change_role`, `test_superadmin_can_change_role`)
- **IAM-USER-3.** Assigning the `superadmin` role is refused 400, so the startup seed is
  the only way an account becomes superadmin. \
  Enforced in: `services/auth/app/routers/admin.py` (`update_user_role`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_cannot_set_superadmin_role`)
- **IAM-USER-4.** Changing one's own role is refused 400, before the target is looked
  up. \
  Enforced in: `services/auth/app/routers/admin.py` (`update_user_role`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_cannot_change_own_role`)
- **IAM-USER-5.** An unknown target answers 404 and a target holding the superadmin role
  answers 400. \
  Enforced in: `services/auth/app/routers/admin.py` (`update_user_role`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_change_role_nonexistent_user`, `test_cannot_change_role_of_another_superadmin`)
- **IAM-USER-6.** Setting the role an account already has answers 200 and changes
  nothing else. \
  Enforced in: `services/auth/app/services/auth_service.py` (`set_user_role`) \
  Pinned by: `services/auth/tests/test_auth.py` (`test_set_role_to_same_role`)
- **IAM-USER-7.** A role change records the caller in `modified_by`; it does not revoke
  sessions, so the new role reaches the account's tokens at their next refresh
  (IAM-JWT-2) and binds in auth at once (IAM-ROLE-2). \
  Enforced in: `services/auth/app/services/auth_service.py` (`set_user_role`) \
  Pinned by: `services/auth/tests/test_auth_service_unit.py` (`test_set_user_role_success`)
- **IAM-USER-8.** The Users page shows promote and demote controls only to a superadmin,
  and disables them on the viewer's own row and on the superadmin's row. \
  Enforced in: `frontend/src/pages/admin/UsersPage.tsx` (`UsersPage`); `frontend/src/components/admin/UserManagementTable.tsx` (`UserManagementTable`) \
  Pinned by: `frontend/src/test/pages/UsersPage.test.tsx` (`enables role controls only for a superadmin`, `renders the heading and table for a regular admin without role controls`); `tests/e2e/test_roles_playwright.py` (`test_superadmin_promotes_then_demotes_user`)
- **IAM-USER-9.** While `AUTH_METHOD=ldap`, no login yields a superadmin session: the
  only superadmin accounts are seeded local ones (IAM-USER-3, IAM-BOOT-5), a local
  account cannot log in (IAM-LDAP-1), and sync never changes a role (IAM-SYNC-18). A
  role change (IAM-USER-2) then needs a superadmin refresh token issued before the
  switch or a superadmin API token, because neither refresh nor token exchange checks
  the mode. \
  Enforced in: `services/auth/app/services/auth_service.py` (`authenticate_user`, `rotate_refresh_token`); `services/auth/app/services/api_token_service.py` (`exchange_api_token`); `services/auth/app/routers/admin.py` (`update_user_role`) \
  Pinned by: none

**Out of scope.** The user interface has no activate or deactivate control and no API
token page; those exist only through the API. Nothing deletes an account.

### 8.9 User groups

**What it does.** Admins organize users into named teams. Groups drive which devices a
user can see (`inventory.md`) and which resources ACL grants open to them (section 8.14).

**Surfaces.** User interface `frontend/src/pages/admin/GroupsPage.tsx`,
`frontend/src/pages/admin/GroupDetailPage.tsx`; the `/groups` routes in section 5.

**Rules.**

- **IAM-GROUP-1.** Any signed-in account lists all groups (id, name, description,
  timestamps), without members. \
  Enforced in: `services/auth/app/routers/groups.py` (`list_groups`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_list_groups`)
- **IAM-GROUP-2.** A group's detail with its members' usernames and emails is admin and
  superadmin only. By decision; see issue #711. \
  Enforced in: `services/auth/app/routers/groups.py` (`get_group`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_get_group_user_forbidden`, `test_get_group_admin_sees_members_with_email`)
- **IAM-GROUP-3.** Group names are 1 to 100 characters and unique (409 `A group with
  this name already exists` on create or rename); descriptions are at most 2000
  characters. \
  Enforced in: `services/auth/app/schemas/group.py` (`GroupCreateRequest`, `GroupUpdateRequest`); `services/auth/app/routers/groups.py` (`create_group_endpoint`, `update_group_endpoint`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_create_group_duplicate_name`, `test_create_group_empty_name`, `test_update_group_duplicate_name`); `services/auth/tests/test_schema_bounds.py` (`test_group_description_over_cap_rejected`)
- **IAM-GROUP-4.** An update changes only the fields it sends and records the caller in
  `modified_by`. \
  Enforced in: `services/auth/app/services/group_service.py` (`update_group`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_update_group_partial_name_only`); `services/auth/tests/test_groups_service_unit.py` (`test_update_group_with_modified_by`)
- **IAM-GROUP-5.** Deleting a group deletes its memberships and its directory mapping;
  its former members are not added back to "Not Grouped". \
  Enforced in: `services/auth/app/services/group_service.py` (`delete_group`); `services/auth/app/models/group.py` (`UserGroup`); `services/auth/app/models/ldap_group_mapping.py` (`LdapGroupMapping`) \
  Pinned by: none
- **IAM-GROUP-6.** Adding a member answers 404 for an unknown group or user and 409 for
  an existing membership; adding to any group other than "Not Grouped" removes the user
  from "Not Grouped". \
  Enforced in: `services/auth/app/routers/groups.py` (`add_member_endpoint`); `services/auth/app/services/group_service.py` (`_add_member_resolved`, `_remove_from_not_grouped`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_add_member_user_not_found`, `test_add_member_group_not_found`, `test_add_member_duplicate`, `test_add_member_removes_from_not_grouped`, `test_add_to_not_grouped_does_not_remove`)
- **IAM-GROUP-7.** Removing a member answers 404 for an unknown group or membership; a
  user removed from their last group is not added back to "Not Grouped". By decision;
  see ADR 0011, Blast radius. \
  Enforced in: `services/auth/app/routers/groups.py` (`remove_member_endpoint`); `services/auth/app/services/group_service.py` (`remove_member`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_remove_member`, `test_remove_member_not_found`, `test_remove_member_group_not_found`)
- **IAM-GROUP-8.** Bulk add takes at most 500 ids, skips ids that are already members,
  removes the added users from "Not Grouped", and answers `{added, skipped}`; an
  unknown group answers 404. \
  Enforced in: `services/auth/app/services/group_service.py` (`bulk_add_members`); `services/auth/app/schemas/group.py` (`BulkAddMembersRequest`); `services/auth/app/routers/groups.py` (`bulk_add_members_endpoint`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_bulk_add_members_success`, `test_bulk_add_members_skips_duplicates`, `test_bulk_add_removes_from_not_grouped`); `services/auth/tests/test_schema_bounds.py` (`test_bulk_add_members_over_cap_rejected`); `services/auth/tests/test_routers_direct.py` (`test_groups_bulk_add_group_not_found`)
- **IAM-GROUP-13.** A bulk add that repeats an id counts the repeat as skipped. An id
  with no account fails the whole request on the database's foreign key (an unhandled
  error, 500) and nothing is added; unlike single add (IAM-GROUP-6) there is no 404 for
  it. Known gap, see #1009. \
  Enforced in: `services/auth/app/services/group_service.py` (`bulk_add_members`) \
  Pinned by: none
- **IAM-GROUP-9.** Bulk remove takes at most 500 ids and answers `{removed, not_found}`;
  an unknown group answers 404. \
  Enforced in: `services/auth/app/services/group_service.py` (`bulk_remove_members`); `services/auth/app/routers/groups.py` (`bulk_remove_members_endpoint`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_bulk_remove_members_success`, `test_bulk_remove_members_not_found`, `test_bulk_remove_members_group_not_found`); `services/auth/tests/test_schema_bounds.py` (`test_bulk_remove_members_over_cap_rejected`)
- **IAM-GROUP-10.** Any signed-in account may read the groups of any user id, one at a
  time or up to 500 at once; an unknown or groupless id answers an empty list, and the
  batch answer has a key for every requested id. By decision; see the API Reference by
  Role table in [ROLES.md](../ROLES.md). \
  Enforced in: `services/auth/app/routers/groups.py` (`get_user_groups_endpoint`, `get_users_groups_endpoint`); `services/auth/app/services/group_service.py` (`get_user_groups_map`) \
  Pinned by: `services/auth/tests/test_groups.py` (`test_get_user_groups_nonexistent_user`, `test_get_users_groups_batch`, `test_get_users_groups_batch_unknown_user`); `services/auth/tests/test_groups_service_unit.py` (`test_get_user_groups_map_includes_user_with_no_groups`)
- **IAM-GROUP-11.** "Not Grouped" is found by its exact name every time; renaming or
  deleting it turns the automatic assignment off until the next service start recreates
  a group of that name. \
  Enforced in: `services/auth/app/services/group_service.py` (`get_group_by_name`); `services/auth/app/main.py` (`_seed_not_grouped`) \
  Pinned by: none
- **IAM-GROUP-12.** The group detail page does not fetch the user list for a non-admin
  viewer. \
  Enforced in: `frontend/src/pages/admin/GroupDetailPage.tsx` (`GroupDetailPage`) \
  Pinned by: `frontend/src/test/pages/GroupDetailPage.test.tsx` (`does not fetch the user list for a non-admin viewer`)

**Out of scope.** Device groups and device visibility are in `inventory.md`.

### 8.10 Directory group mappings

**What it does.** On an LDAP deployment, an admin links a directory group to a HERD
group so the HERD group's membership follows the directory.

**Surfaces.** User interface `frontend/src/pages/admin/LdapSyncPage.tsx`; routes under
`/admin/ldap-sync/mappings` (`services/auth/app/routers/ldap_sync.py`).

**Rules.**

- **IAM-MAP-1.** Creating a mapping needs `AUTH_METHOD=ldap` (409 otherwise) and an
  existing HERD group (404). \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`create_mapping`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_create_mapping_refused_outside_ldap_mode`, `test_create_mapping_unknown_herd_group_is_404`)
- **IAM-MAP-2.** A DN or a HERD group that is already mapped answers 409 before the
  directory is asked. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`create_mapping`, `_conflicting_mapping_detail`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_create_mapping_duplicate_dn_is_409`, `test_create_mapping_herd_group_already_mapped_is_409`); `services/auth/tests/test_routers_direct_ldap_admin.py` (`test_create_mapping_direct_duplicate_dn_precheck_409`, `test_create_mapping_direct_group_already_mapped_precheck_409`)
- **IAM-MAP-3.** The DN is checked against the live directory: a directory that cannot
  be asked answers 503, and a DN the directory proves resolves nothing (no such entry,
  or invalid DN syntax) answers 422. The 503 detail is `Directory unavailable, mapping
  not validated:` followed by the directory client's error text, which can include the
  text of the underlying exception. Known gap, see #1009. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`create_mapping`); `services/auth/app/services/ldap_service.py` (`fetch_group`, `_base_entry`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_create_mapping_dangling_dn_is_422`, `test_create_mapping_directory_outage_is_503_not_422`); `services/auth/tests/test_ldap_service_live.py` (`test_fetch_group_nonexistent_dn_is_dangling_none`, `test_fetch_group_invalid_dn_syntax_is_proven_unresolvable`)
- **IAM-MAP-4.** The stored DN is the canonical DN the directory returned, not the typed
  one; the cached display name is the group's `LDAP_GROUP_NAME_ATTRIBUTE` cut to 255
  characters, or the DN when the attribute is missing. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`create_mapping`); `services/auth/app/services/ldap_service.py` (`_fetch_group_sync`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_create_mapping_stores_canonical_directory_dn`, `test_create_mapping_truncates_long_directory_name`); `services/auth/tests/test_ldap_service.py` (`test_fetch_group_missing_name_attribute_falls_back_to_dn`)
- **IAM-MAP-5.** An entry with no members is accepted with a `warning` in the response,
  never refused. By decision; see ADR 0011, the 2026-08-12 amendment. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`create_mapping`, `NO_MEMBERS_WARNING`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_create_mapping_with_no_members_warns_but_succeeds`)
- **IAM-MAP-6.** One directory group maps to at most one HERD group and one HERD group to
  at most one directory group. A create that loses a race answers 404 when the HERD group
  vanished, 409 naming the constraint when one now conflicts, and 409 `Mapping conflicts
  with concurrent changes; retry` otherwise. \
  Enforced in: `services/auth/app/models/ldap_group_mapping.py` (`LdapGroupMapping`); `services/auth/app/routers/ldap_sync.py` (`create_mapping`) \
  Pinned by: `services/auth/tests/test_routers_direct_ldap_admin.py` (`test_create_mapping_direct_race_group_deleted_after_lookup`, `test_create_mapping_direct_race_concurrent_duplicate`, `test_create_mapping_direct_race_no_matching_constraint_falls_back`)
- **IAM-MAP-7.** Mapping validation and every sync directory call bind as the service
  account; an empty `LDAP_BIND_DN` counts as the directory being unavailable, never as
  an anonymous bind. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_open_service_connection`, `_call_with_connection`) \
  Pinned by: `services/auth/tests/test_ldap_service_live.py` (`test_sync_client_anonymous_bind_refused`, `test_sync_client_bad_service_account_raises`)
- **IAM-MAP-8.** Listing (oldest first) and deleting work in any auth mode, so stale
  mappings can be cleaned up; deleting an unknown mapping answers 404. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`list_mappings`, `delete_mapping`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_list_and_delete_work_outside_ldap_mode`, `test_list_mappings_paginates`, `test_delete_missing_mapping_is_404`)
- **IAM-MAP-9.** Every mapping and run route is admin and superadmin only, and 401 to an
  unauthenticated caller. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`_admin_or_superadmin`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_non_admin_is_403_on_all_mapping_endpoints`, `test_non_admin_is_403_on_all_run_endpoints`, `test_status_non_admin_is_403`, `test_unauthenticated_is_401`)

**Out of scope.** A renamed or moved directory group leaves its mapping dangling until
an admin re-creates it; stable directory ids are not tracked (ADR 0011, Out of scope).

### 8.11 Directory group sync

**What it does.** A sync pass makes each mapped HERD group's LDAP members match the
directory group, creating HERD accounts for directory members who have none. It never
removes anyone when the directory could not be read completely.

**Surfaces.** Route `POST /admin/ldap-sync/run`, the interval loop (section 8.13), and
`services/auth/app/services/ldap_sync_service.py`.

**Rules.**

- **IAM-SYNC-1.** A pass reconciles every mapping, oldest first, over one shared
  directory connection for the whole run. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`execute_run`); `services/auth/app/services/ldap_service.py` (`run_connection`, `RunConnection`) \
  Pinned by: `services/auth/tests/test_ldap_service_run_connection.py` (`test_shared_run_connection_one_bind_for_three_mappings`, `test_run_with_mapping_and_sweep_shares_one_connection`)
- **IAM-SYNC-2.** A group whose entry cannot be fetched is skipped whole
  (`directory_unavailable`), a DN that resolves nothing is skipped whole (`dangling_dn`),
  and a group where any member DN could not be resolved is skipped whole
  (`member_resolution_unavailable`); a skipped group gets zero changes. By decision; see
  ADR 0011, Reconciliation. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`); `services/auth/app/services/ldap_service.py` (`resolve_members`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_fetch_error_skips_whole_group_with_zero_changes`, `test_dangling_dn_skips_whole_group_with_zero_changes`, `test_member_resolution_error_skips_whole_group`)
- **IAM-SYNC-3.** A successful group fetch refreshes the mapping's cached display name. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`) \
  Pinned by: none
- **IAM-SYNC-4.** A member the directory proves unusable is counted and skipped without
  failing the group: `not_found` (entry gone), `missing_email`, `missing_username`. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_resolution_for`); `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_member_skip_reasons_counted_without_failing_group`); `services/auth/tests/test_ldap_service_live.py` (`test_resolve_member_skip_reasons`)
- **IAM-SYNC-5.** When any member of a group skipped as `missing_email` or
  `missing_username`, no member is removed from that group this pass (recorded as
  `suppressed_removals` when removals were due); a `not_found` skip never suppresses.
  By decision; see ADR 0011, phase 3 amendment 1. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`, `_REMOVAL_SUPPRESSING_REASONS`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_missing_email_skip_suppresses_group_removals`, `test_not_found_skip_does_not_suppress_removal`)
- **IAM-SYNC-6.** A member is matched to a HERD account by email. A member with no
  account is provisioned (LDAP, no password, role user) and then added. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_ensure_ldap_user`, `_provision_or_recover`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_preprovision_creates_ldap_user_without_password`)
- **IAM-SYNC-7.** A provisioning collision whose retry lookup finds the email (a
  concurrent login created it) proceeds and is recorded as `provision_races`; one that
  finds nothing (the username belongs to another account) is skipped as
  `username_taken`. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_provision_or_recover`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_provision_race_recovered_via_lookup_retry`, `test_collision_username_taken_skips_member`)
- **IAM-SYNC-8.** A member whose email belongs to a local account is skipped as
  `email_owned_by_local_account`; local accounts in a mapped group are never added or
  removed by sync. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_classify_auth_source_and_active`, `_reconcile_mapping`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_collision_email_owned_by_local_account_skips_member`, `test_local_group_member_never_removed`)
- **IAM-SYNC-9.** An inactive LDAP account is invisible to sync in both directions:
  skipped as `user_inactive` when listed, and never removed when already a member. By
  decision; see ADR 0011, phase 3 amendment 2. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_classify_auth_source_and_active`, `_reconcile_mapping`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_inactive_ldap_member_survives_and_is_not_readded`, `test_inactive_ldap_member_already_grouped_is_untouched`, `test_inactive_ldap_user_is_not_added`)
- **IAM-SYNC-10.** Adds are the resolved members not yet in the group, removals are the
  group's active LDAP members no longer listed; both go through the same group
  operations as manual administration, including the "Not Grouped" removal. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`); `services/auth/app/services/group_service.py` (`_add_member_resolved`, `remove_member`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_set_difference_applies_exact_diff_through_group_service`); `services/auth/tests/test_ldap_service_live.py` (`test_run_sync_herd_eng_builds_membership_from_live_directory`)
- **IAM-SYNC-11.** Right before adding, each account is re-read: one deleted since it
  was resolved is skipped as `user_missing` and loses its presence credit (IAM-SWEEP-4);
  one deactivated since is skipped as `user_inactive` and keeps it. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`, `_reverify_still_present`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service_scaling.py` (`test_deleted_mid_run_records_missing_not_inactive_and_drops_credit`, `test_deactivate_mid_run_does_not_add`, `test_reactivate_mid_run_does_not_remove`)
- **IAM-SYNC-12.** A stored username that differs from the directory's is updated; when
  the new name is taken, only the rename is skipped (`drift_collisions`, run `partial`)
  and the member is still reconciled. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_finalize_ldap_user`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_username_drift_repaired`, `test_username_drift_collision_skips_repair_not_membership`)
- **IAM-SYNC-13.** One failing add or remove is isolated: an add refused because the
  membership already exists is a no-op, any other failure is recorded under
  `op_failures`, and the pass continues. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_add_integrity_error_with_existing_row_is_benign_noop`, `test_add_integrity_error_without_row_is_op_failure`, `test_other_op_failure_is_isolated_and_loop_continues`)
- **IAM-SYNC-14.** Within one run an email resolved in one group is reused in later
  groups; only a success and `email_owned_by_local_account` are remembered, every other
  skip is re-checked per group. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_cache_outcome`, `_CACHEABLE_SKIP_REASONS`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service_scaling.py` (`test_cache_outcome_only_caches_the_structural_frozenset`, `test_shared_member_across_two_groups_resolved_once`, `test_username_taken_skip_not_memoized_across_groups`)
- **IAM-SYNC-15.** A second pass against an unchanged directory changes nothing. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_second_run_against_unchanged_directory_is_noop`); `services/auth/tests/test_ldap_service_live.py` (`test_run_sync_second_run_is_idempotent`)
- **IAM-SYNC-16.** Each detail category keeps at most 20 records followed by a
  `{"truncated": N}` marker. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`DETAIL_CAP`, `_CappedCategory`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_detail_categories_cap_with_truncation_marker`)
- **IAM-SYNC-17.** On the shared connection, only a transport failure on a connection
  that was already open is retried, once, on a fresh connection; a first-bind failure
  or a directory error code is not retried. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`_call_with_connection`, `_is_transport_failure`) \
  Pinned by: `services/auth/tests/test_ldap_service_run_connection.py` (`test_dropped_connection_reconnects_once_and_succeeds`, `test_persistent_transport_failure_raises_after_exactly_one_retry`, `test_non_benign_result_code_on_live_connection_does_not_retry`, `test_initial_bind_failure_attempts_exactly_one_connect_no_retry`)
- **IAM-SYNC-18.** Sync never changes a role. By decision; see ADR 0011, Blast radius. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_reconcile_mapping`) \
  Pinned by: none

**Out of scope.** Nested groups and `memberUid` groups are not followed (ADR 0011).

### 8.12 Deactivation and reactivation sweep

**What it does.** When enabled, each sync pass also turns off the HERD accounts of
people who left the directory or were disabled there, and turns back on accounts it
turned off earlier once they reappear. A circuit breaker stops a mass deactivation.

**Surfaces.** The end of every sync pass; settings `LDAP_SYNC_DEACTIVATION_ENABLED`,
`LDAP_DISABLED_FILTER`, `LDAP_SYNC_DEACTIVATION_MAX_PERCENT`,
`LDAP_SYNC_DEACTIVATION_MIN_COUNT`. The status writes are IAM-ACCT-3 and IAM-ACCT-4.

**Rules.**

- **IAM-SWEEP-1.** With `LDAP_SYNC_DEACTIVATION_ENABLED` false (the default) the sweep
  does nothing and records nothing. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_run_deactivation_sweep`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_disabled_when_setting_off`)
- **IAM-SWEEP-2.** Presence is one paged enumeration of every email attribute value
  (lowercased) under `LDAP_USER_BASE_DN`; a failed page or a non-success result raises
  instead of returning a partial set. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`present_emails`, `_paged_user_emails_sync`) \
  Pinned by: `services/auth/tests/test_ldap_service.py` (`test_present_emails_collects_across_multiple_pages_lowercased`, `test_present_emails_collects_multivalued_attribute`, `test_present_emails_missing_control_after_success_ends_pages`, `test_present_emails_raises_on_non_success_page`, `test_present_emails_raises_on_ldap_exception`)
- **IAM-SWEEP-3.** When `LDAP_DISABLED_FILTER` is set it must start with `(`, and the
  emails it matches (with an email attribute) form the disabled set. \
  Enforced in: `services/auth/app/services/ldap_service.py` (`disabled_emails`, `_disabled_emails_sync`) \
  Pinned by: `services/auth/tests/test_ldap_service.py` (`test_disabled_emails_conjoins_filter_with_presence`, `test_disabled_emails_filter_not_starting_with_paren_raises_value_error`, `test_disabled_emails_empty_filter_raises_value_error`)
- **IAM-SWEEP-4.** An account counts as present when it was resolved as a member of any
  mapped group this pass or its lowercased email is in the presence set, and it is not
  in the disabled set; disabled wins over both. By decision; see ADR 0011, phase 4
  amendment 2. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_run_deactivation_sweep`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_group_presence_credit_beats_absent_enumeration`, `test_sweep_disabled_filter_deactivates_even_when_present_and_credited`)
- **IAM-SWEEP-5.** A failed enumeration changes no account in either direction and
  marks the run `partial` (`enumeration_unavailable`). \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_run_deactivation_sweep`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_enumeration_failure_touches_no_one`)
- **IAM-SWEEP-6.** Candidates are every LDAP account, active or inactive, whatever its
  role; active ones not present are to be deactivated, and inactive ones with sync
  provenance that are present are to be reactivated. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_run_deactivation_sweep`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_provenance_gate`, `test_sweep_counters_and_detail_apply_to_run_row`)
- **IAM-SWEEP-7.** The breaker trips only when the deactivation count (active LDAP
  accounts found not present) is strictly greater than
  `LDAP_SYNC_DEACTIVATION_MAX_PERCENT` percent of every LDAP account, active or inactive
  and whatever its role (the IAM-SWEEP-6 candidates), and strictly greater than
  `LDAP_SYNC_DEACTIVATION_MIN_COUNT`; a tripped breaker deactivates no one but still
  applies the reactivations. By decision: ADR 0011 (as amended 2026-08-12) makes the
  sweep one pass over all LDAP users, so every candidate is swept and counts in the
  denominator; phase 4 amendment 1 sets the strict comparison and Reactivation the
  exempt reactivations. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_run_deactivation_sweep`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_breaker_boundary_equal_min_count_applies`, `test_sweep_breaker_boundary_equal_percent_applies`, `test_sweep_breaker_both_terms_exceeded_aborts_but_still_reactivates`)
- **IAM-SWEEP-8.** Flips are counted and recorded only after their single commit and
  only for rows the guarded update changed; a failed commit records none. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_apply_sweep_flips`) \
  Pinned by: `services/auth/tests/test_ldap_sync_service.py` (`test_sweep_commit_failure_records_no_counters`)
- **IAM-SWEEP-9.** The sweep's deactivation and reactivation work end to end against a
  real directory. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_run_deactivation_sweep`) \
  Pinned by: `services/auth/tests/test_ldap_service_live.py` (`test_live_deactivation_and_reactivation_sweep`)

**Out of scope.** Deactivation never deletes an account and never revokes tokens; the
inactive checks of IAM-ACCT-5 do the blocking.

### 8.13 Sync runs, sync-now, and the interval loop

**What it does.** An admin starts a sync on demand and watches its progress; a
deployment can also run sync on a schedule. Only one sync runs at a time, across every
copy of the auth service.

**Surfaces.** User interface `frontend/src/pages/admin/LdapSyncPage.tsx`; routes
`POST /admin/ldap-sync/run`, `GET /admin/ldap-sync/runs`,
`GET /admin/ldap-sync/runs/{id}`, `GET /admin/ldap-sync/status`; background task
`services/auth/app/tasks/ldap_sync_loop.py` (interval `LDAP_SYNC_INTERVAL_SECONDS`).

**Rules.**

- **IAM-SERIAL-1.** A run holds a slot made of an in-process lock and, on Postgres, a
  session advisory lock on the fixed key `herd_ldap_group_sync`; a second run in the same
  process is refused as busy `in_process` and one on another replica as busy `replica`,
  each mapped by sync-now to its own 409. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_SyncSlot`, `SyncBusyError`, `_ADVISORY_LOCK_KEY`); `services/auth/app/routers/ldap_sync.py` (`start_sync_run`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_sync_run_409_while_in_progress_and_lock_released_after`); `services/auth/tests/test_routers_direct_ldap_admin.py` (`test_start_sync_run_direct_in_process_busy_409`, `test_start_sync_run_direct_replica_busy_409`); `services/auth/tests/test_ldap_sync_service_live_pg.py` (`test_sync_slot_replica_busy_when_another_connection_holds_the_lock`, `test_sync_slot_acquires_cleanly_once_the_other_replica_releases`)
- **IAM-SERIAL-2.** Sync-now is refused 409 outside LDAP mode without taking the slot;
  otherwise it creates the run row, hands the slot to a background task, and answers 202
  with `run_id` at once. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`start_sync_run`); `services/auth/app/services/ldap_sync_service.py` (`start_background_run`, `_run_in_background`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_sync_run_202_then_run_visible_and_finalized`, `test_sync_run_refused_outside_ldap_mode_without_leaking_lock`); `tests/integration/test_ldap_sync_admin.py` (`test_concurrent_sync_now_one_wins`)
- **IAM-SERIAL-3.** A failed advisory unlock invalidates the lock's connection so the
  session lock cannot outlive the run on a pooled connection. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`_release_sync_locks`) \
  Pinned by: none
- **IAM-REAP-1.** The stale-run reaper runs at the start of every run, inside the slot,
  on its own session; a busy slot reaps nothing, and a reaper failure is logged and
  never fails the run. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`run_sync`, `start_background_run`, `_reap_stale_running_runs_on_own_session`) \
  Pinned by: `services/auth/tests/test_ldap_sync_stale_run_reaper.py` (`test_run_sync_reaps_inside_the_slot_and_survives_a_reaper_failure`, `test_run_sync_on_a_busy_slot_does_not_reap`, `test_start_background_run_reaps_inside_the_slot_and_survives_a_failure`, `test_start_background_run_on_a_busy_slot_does_not_reap`, `test_own_session_reap_swallows_a_raising_reaper`)
- **IAM-REAP-2.** The stale threshold is `LDAP_SYNC_RUN_STALE_SECONDS` (default 7200),
  raised with a warning to the larger of 60 seconds and twice the effective interval. \
  Enforced in: `services/auth/app/config.py` (`effective_ldap_sync_run_stale_seconds`) \
  Pinned by: `services/auth/tests/test_ldap_sync_stale_run_reaper.py` (`test_effective_stale_seconds_passes_a_value_above_both_floor_terms_through`, `test_effective_stale_seconds_clamps_below_the_absolute_floor_and_warns`, `test_effective_stale_seconds_clamps_a_value_under_twice_the_interval`, `test_reaper_uses_the_clamped_threshold_not_the_raw_setting`)
- **IAM-LOOP-1.** The interval loop starts only when `AUTH_METHOD=ldap` and
  `LDAP_GROUP_SYNC_ENABLED` is true, and is cancelled at shutdown. \
  Enforced in: `services/auth/app/tasks/ldap_sync_loop.py` (`ldap_group_sync_loop_enabled`); `services/auth/app/main.py` (`lifespan`) \
  Pinned by: `services/auth/tests/test_ldap_sync_loop.py` (`test_loop_enabled_requires_both_ldap_method_and_flag`, `test_loop_disabled_when_flag_off_even_in_ldap_mode`, `test_loop_disabled_in_local_mode_even_when_flag_on`); `services/auth/tests/test_main.py` (`test_lifespan_starts_ldap_sync_loop_when_enabled`, `test_lifespan_skips_ldap_sync_loop_when_disabled`)
- **IAM-LOOP-2.** An interval below 60 seconds is raised to 60 with a warning; the
  service still starts. \
  Enforced in: `services/auth/app/config.py` (`effective_ldap_sync_interval_seconds`, `MIN_LDAP_SYNC_INTERVAL_SECONDS`) \
  Pinned by: `services/auth/tests/test_ldap_sync_loop.py` (`test_effective_interval_seconds_clamps_and_warns_below_floor`, `test_effective_interval_seconds_clamps_zero`, `test_effective_interval_seconds_passes_through_at_or_above_floor`)
- **IAM-LOOP-3.** The first tick waits a full interval before syncing; each tick runs a
  run with trigger `interval`. \
  Enforced in: `services/auth/app/tasks/ldap_sync_loop.py` (`ldap_sync_loop`, `_run_interval_tick`) \
  Pinned by: `services/auth/tests/test_ldap_sync_loop.py` (`test_first_tick_sleeps_before_syncing`, `test_tick_invokes_run_sync_with_interval_trigger`)
- **IAM-LOOP-4.** A busy slot is logged as a skipped tick, and any other tick failure is
  logged; the loop never stops on either. \
  Enforced in: `services/auth/app/tasks/ldap_sync_loop.py` (`ldap_sync_loop`, `_run_interval_tick`) \
  Pinned by: `services/auth/tests/test_ldap_sync_loop.py` (`test_sync_busy_error_is_swallowed_as_skip`, `test_tick_exception_is_logged_and_loop_continues`)
- **IAM-LOOP-5.** The loop deletes run rows older than `LDAP_SYNC_RUNS_RETENTION_DAYS`
  (default 90) that are not `running`, on its first tick and then at most once per 24
  hours; a failed prune is retried on the next tick. \
  Enforced in: `services/auth/app/tasks/ldap_sync_loop.py` (`_prune_old_runs`, `ldap_sync_loop`, `_PRUNE_INTERVAL`) \
  Pinned by: `services/auth/tests/test_ldap_sync_loop.py` (`test_prune_old_runs_deletes_only_old_non_running_rows`, `test_first_tick_prunes_because_last_prune_starts_unseeded`, `test_prune_runs_at_most_once_per_24h_across_two_ticks`, `test_failed_prune_retries_next_tick_succeeding_prune_then_rate_limits`)
- **IAM-LOOP-6.** Sync-now never prunes, so a deployment without the loop keeps every
  run row. By decision; see the module docstring of `ldap_sync_loop.py` and ADR 0011,
  Audit. \
  Enforced in: `services/auth/app/services/ldap_sync_service.py` (`start_background_run`) \
  Pinned by: none
- **IAM-LOOP-7.** The status route answers `{auth_method, group_sync_enabled,
  sync_interval_seconds}` in any auth mode, with the effective interval, not the raw
  setting. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`get_sync_status`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_status_reports_ldap_mode`, `test_status_reports_local_mode`, `test_status_reports_the_clamped_interval_not_the_raw_setting`, `test_status_never_409s_under_local_mode`)
- **IAM-LOOP-8.** Runs list newest first (id breaks ties); an unknown run id answers 404. \
  Enforced in: `services/auth/app/routers/ldap_sync.py` (`list_sync_runs`, `get_sync_run`) \
  Pinned by: `services/auth/tests/test_ldap_sync.py` (`test_list_runs_paginates_newest_first`, `test_get_missing_run_is_404`)
- **IAM-LOOP-9.** The admin page disables mapping create and sync-now unless the status
  says LDAP mode, and shows the interval only when the loop is on. \
  Enforced in: `frontend/src/pages/admin/LdapSyncPage.tsx` (`LdapSyncPage`) \
  Pinned by: `frontend/src/test/pages/LdapSyncPage.test.tsx` (`local-auth mode shows the inactive banner and disables create/sync-now`, `ldap mode with the loop enabled shows the interval and enables actions`, `ldap mode with the loop disabled notes manual-only sync`)
- **IAM-LOOP-10.** The page shows a `running` row older than 30 minutes as `running
  (stale)` and polls the runs list every 2 seconds while any run is running and not
  stale. The 30 minutes is a display hint below the reaper's threshold by decision
  (docstring of `reap_stale_running_runs`). \
  Enforced in: `frontend/src/api/ldapSync.ts` (`STALE_RUNNING_THRESHOLD_MS`, `isRunStale`, `runsRefetchInterval`) \
  Pinned by: `frontend/src/test/api/ldapSync.test.ts` (`is true for a running row older than the staleness threshold`, `is false exactly at the threshold boundary (strict exceeds)`, `polls every 2s while a run is actively running`, `stops polling once the only running row is stale`)
- **IAM-LOOP-11.** The page treats sync-now's two busy 409s as information and every
  other failure, including the mode refusal, as an error. \
  Enforced in: `frontend/src/pages/admin/LdapSyncPage.tsx` (`LdapSyncPage`) \
  Pinned by: `frontend/src/test/pages/LdapSyncPage.test.tsx` (`sync now shows an informational (non-error) toast on the in-process 409 lock detail`, `sync now shows an informational toast on the cross-replica 409 lock detail`, `sync now surfaces the mode-refusal 409 as an error, not informational`)

**Out of scope.** Event-driven sync and SCIM do not exist (ADR 0011, Out of scope).

### 8.14 Resource-level ACL grants

**What it does.** An admin gives a user group view or manage access to one device,
topology, reservation, or secret. Other services ask the acl service whether a user
holds such a grant.

**Surfaces.** User interface `frontend/src/pages/admin/GrantsPage.tsx`; the acl routes
in section 5 (`services/acl/app/routers/grants.py`).

**Rules.**

- **IAM-ACL-1.** A grant names a group, a resource type in {`device`, `topology`,
  `reservation`, `secret`}, a resource id, and a permission in {`view`, `manage`}; any
  other type or permission answers 422. \
  Enforced in: `services/acl/app/schemas/grant.py` (`GrantCreateRequest`, `VALID_RESOURCE_TYPES`, `VALID_PERMISSIONS`) \
  Pinned by: `services/acl/tests/test_grants.py` (`test_create_grant_invalid_resource_type`, `test_create_grant_invalid_permission`)
- **IAM-ACL-2.** A grant is unique on group, type, resource, and permission (409 `This
  grant already exists`); the same resource with the other permission is a separate
  grant. \
  Enforced in: `services/acl/app/models/grant.py` (`ResourceGrant`); `services/acl/app/routers/grants.py` (`create_grant_endpoint`) \
  Pinned by: `services/acl/tests/test_grants_errors.py` (`test_create_duplicate_grant_returns_409_with_detail`, `test_create_grant_same_resource_different_permission_is_not_conflict`)
- **IAM-ACL-3.** Neither the group nor the resource is checked for existence; a grant
  for an unknown id is stored. \
  Enforced in: `services/acl/app/services/grant_service.py` (`create_grant`) \
  Pinned by: `services/acl/tests/test_grants.py` (`test_grant_for_nonexistent_group`)
- **IAM-ACL-4.** Creating, listing, reading, and deleting grants need an `admin` or
  `superadmin` role claim (403 otherwise, 401 without a valid token). \
  Enforced in: `services/acl/app/routers/grants.py` (`require_admin`) \
  Pinned by: `services/acl/tests/test_grants.py` (`test_create_grant_user_forbidden`, `test_create_grant_unauthenticated`, `test_list_grants_user_forbidden`, `test_delete_grant_user_forbidden`)
- **IAM-ACL-5.** `granted_by` is the caller's `sub`. \
  Enforced in: `services/acl/app/routers/grants.py` (`create_grant_endpoint`) \
  Pinned by: `services/acl/tests/test_grants_handlers_direct.py` (`test_create_grant_endpoint_returns_grant_with_granted_by`)
- **IAM-ACL-6.** The list filters by any of `group_id`, `resource_type`, `resource_id`
  and is ordered oldest first. \
  Enforced in: `services/acl/app/services/grant_service.py` (`list_grants`) \
  Pinned by: `services/acl/tests/test_grants.py` (`test_list_grants_filter_group_id`, `test_list_grants_filter_resource_type`, `test_list_grants_filter_resource_id`, `test_list_grants_filter_combined`); `services/acl/tests/test_grant_service_unit.py` (`test_list_grants_orders_by_granted_at_ascending`)
- **IAM-ACL-7.** A check is allowed when any of the user's groups holds a grant on that
  exact type and resource with the requested permission; a `view` check also accepts
  `manage`, a `manage` check does not accept `view`. The answer lists the matching
  grants. \
  Enforced in: `services/acl/app/services/grant_service.py` (`check_permission`) \
  Pinned by: `services/acl/tests/test_grants.py` (`test_check_allowed`, `test_check_denied_no_grant`, `test_check_denied_wrong_permission`, `test_check_manage_implies_view`, `test_check_denied_wrong_group`); `services/acl/tests/test_grant_service_unit.py` (`test_check_permission_manage_request_does_not_match_view_grant`)
- **IAM-ACL-8.** A caller whose role claim is not admin or superadmin may ask only about
  their own `sub` (403 `Cannot query permissions for another user`), on the check, the
  batch check, and the resource list. \
  Enforced in: `services/acl/app/routers/grants.py` (`_authorize_subject`) \
  Pinned by: `services/acl/tests/test_grants.py` (`test_check_rejects_other_user`, `test_check_batch_rejects_other_user`, `test_resources_rejects_other_user`, `test_check_allows_self`, `test_check_admin_may_query_other_user`)
- **IAM-ACL-9.** The user's groups come from auth's `GET /groups/user/{user_id}` with the
  caller's own token; a transport error or a non-200 answer counts as no groups, so the
  check fails closed. \
  Enforced in: `services/acl/app/services/auth_client.py` (`fetch_user_groups`); `services/acl/app/services/grant_service.py` (`check_permission`) \
  Pinned by: `services/acl/tests/test_auth_client.py` (`test_fetch_user_groups_non_200_returns_empty`, `test_fetch_user_groups_500_returns_empty`, `test_fetch_user_groups_connection_error_returns_empty`, `test_fetch_user_groups_timeout_returns_empty`); `services/acl/tests/test_grants.py` (`test_check_fetch_user_groups_returns_empty_allows_false`)
- **IAM-ACL-10.** A batch check takes at most 500 resource ids and answers a result for
  every id, keyed by its string form. \
  Enforced in: `services/acl/app/schemas/grant.py` (`BatchCheckRequest`); `services/acl/app/services/grant_service.py` (`batch_check`) \
  Pinned by: `services/acl/tests/test_schema_bounds.py` (`test_batch_check_resource_ids_over_cap_rejected`); `services/acl/tests/test_grant_service_unit.py` (`test_batch_check_no_groups_returns_all_false_keyed_by_str`, `test_batch_check_preserves_input_ordering`)
- **IAM-ACL-11.** The resource list answers each resource id at most once. \
  Enforced in: `services/acl/app/services/grant_service.py` (`get_accessible_resources`) \
  Pinned by: `services/acl/tests/test_grant_service_unit.py` (`test_get_accessible_resources_deduplicates`)
- **IAM-ACL-12.** The internal check evaluates the same way but resolves groups through
  auth's internal groups route, which answers 404 for an unknown or inactive account;
  any failure or a missing token counts as no groups (fails closed). \
  Enforced in: `services/acl/app/routers/internal.py` (`check_permission_internal`); `services/acl/app/services/auth_client.py` (`fetch_user_groups_internal`) \
  Pinned by: `services/acl/tests/test_internal.py` (`test_internal_check_allowed`, `test_internal_check_denied_no_grant`, `test_internal_check_auth_unreachable_denies`); `services/acl/tests/test_auth_client.py` (`test_fetch_user_groups_internal_404_returns_empty`, `test_fetch_user_groups_internal_no_token_returns_empty_without_calling`)
- **IAM-ACL-13.** Deleting a grant takes effect on the next check. \
  Enforced in: `services/acl/app/services/grant_service.py` (`delete_grant`) \
  Pinned by: `services/acl/tests/test_grants.py` (`test_delete_grant_then_check_denied`); `tests/integration/test_acl_flow.py` (`test_delete_grant_denies_access`)
- **IAM-ACL-14.** The Grants page refuses to send a grant without a group or with a
  resource id that is not a UUID. \
  Enforced in: `frontend/src/pages/admin/GrantsPage.tsx` (`GrantsPage`) \
  Pinned by: `frontend/src/test/pages/GrantsPage.test.tsx` (`blocks create and shows a validation toast when no group is selected`, `blocks create and shows a validation toast when resource id is not a uuid`)

**Out of scope.** What each resource's owning service does with a grant is in that
service's area; secrets are in `operations-and-observability.md`.

### 8.15 Service-to-service authentication and shared checks

**What it does.** Services call each other with a shared secret when no user is acting,
and share one implementation of the "has a manage grant or owns an active reservation"
check.

**Surfaces.** `services/common/herd_common/internal_auth.py`,
`services/common/herd_common/acl.py`, and each service's internal routes (section 7).

**Rules.**

- **IAM-INTERNAL-1.** The internal token is compared in constant time, and an empty
  presented or configured value never matches. \
  Enforced in: `services/common/herd_common/internal_auth.py` (`internal_token_matches`) \
  Pinned by: `services/common/tests/test_internal_auth.py` (`test_correct_token_matches`, `test_wrong_token_does_not_match`, `test_empty_configured_token_refuses_even_with_empty_header`, `test_empty_provided_header_refuses`, `test_uses_constant_time_comparison`, `test_prefix_match_does_not_short_circuit_match`)
- **IAM-INTERNAL-2.** On auth's and acl's internal routes, an unset `INTERNAL_API_TOKEN`
  answers 503, a wrong token 403 `Invalid internal token`, and a missing header 422. \
  Enforced in: `services/auth/app/routers/internal.py` (`_require_internal_token`); `services/acl/app/routers/internal.py` (`_require_internal_token`) \
  Pinned by: `services/auth/tests/test_internal.py` (`test_internal_admins_503_when_token_not_configured`, `test_internal_admins_requires_valid_token`, `test_internal_admins_rejects_correct_prefix_token`, `test_internal_admins_requires_token_header`); `services/acl/tests/test_internal.py` (`test_internal_check_503_when_token_not_configured`, `test_internal_check_requires_valid_token`, `test_internal_check_requires_token_header`)
- **IAM-INTERNAL-3.** `GET /internal/admins` lists the ids of active admin and
  superadmin accounts only. \
  Enforced in: `services/auth/app/routers/internal.py` (`list_admin_user_ids`) \
  Pinned by: `services/auth/tests/test_internal.py` (`test_internal_admins_returns_admin_and_superadmin`, `test_internal_admins_excludes_inactive_users`)
- **IAM-INTERNAL-4.** The contact route answers any existing account, active or not, and
  404 for an unknown id; the caller decides whether to send. \
  Enforced in: `services/auth/app/routers/internal.py` (`get_user_contact`) \
  Pinned by: `services/auth/tests/test_internal.py` (`test_internal_user_contact_returns_email_and_username`, `test_internal_user_contact_404_for_unknown_user`)
- **IAM-INTERNAL-5.** The internal groups route answers the same shape as
  `GET /groups/user/{user_id}`, and 404 for an unknown or inactive account. \
  Enforced in: `services/auth/app/routers/internal.py` (`get_user_groups_internal`) \
  Pinned by: `services/auth/tests/test_internal.py` (`test_internal_user_groups_returns_same_shape_as_groups_user_endpoint`, `test_internal_user_groups_404_for_unknown_user`, `test_internal_user_groups_404_for_inactive_user`)
- **IAM-HELPER-1.** `user_has_grant` asks acl `POST /check` with the caller's forwarded
  token and answers false on a transport error, a non-200 answer, or a body it cannot
  read (fails closed). \
  Enforced in: `services/common/herd_common/acl.py` (`user_has_grant`) \
  Pinned by: `services/common/tests/test_acl.py` (`test_acl_service_unreachable_still_tries_reservations`, `test_acl_5xx_falls_through_to_reservations`, `test_malformed_acl_response_falls_through_to_reservations`)
- **IAM-HELPER-2.** The manage check is true when the user holds an explicit device
  `manage` grant (asked only when a bearer token is present) or owns an `ACTIVE`
  reservation containing the device now (asked with the internal token); the
  reservation-owner pass is by decision ([ROLES.md](../ROLES.md), the reservation-owner
  free pass). \
  Enforced in: `services/common/herd_common/acl.py` (`user_has_manage_or_owns_active_reservation`, `_owns_active_reservation`) \
  Pinned by: `services/common/tests/test_acl.py` (`test_explicit_grant_returns_true`, `test_no_explicit_grant_falls_through_to_reservation_check`, `test_no_grant_no_reservation_returns_false`, `test_no_bearer_token_skips_acl_check_and_tries_reservations`)
- **IAM-HELPER-3.** The reservation leg answers false without calling when no internal
  token is configured, and on a transport error, a non-200 answer, or an unreadable
  body. \
  Enforced in: `services/common/herd_common/acl.py` (`_owns_active_reservation`) \
  Pinned by: `services/common/tests/test_acl.py` (`test_no_internal_token_skips_reservation_lookup`, `test_reservations_service_unreachable_returns_false`, `test_reservations_non_200_returns_false`, `test_malformed_reservation_response_returns_false`)
- **IAM-HELPER-4.** The no-user variant asks acl `POST /internal/check`, then the
  reservation leg, both with the internal token, and fails closed on every failure of
  either. \
  Enforced in: `services/common/herd_common/acl.py` (`user_has_manage_or_owns_active_reservation_internal`, `user_has_manage_internal`, `_explicit_acl_manage_internal`) \
  Pinned by: `services/common/tests/test_acl.py` (`test_manage_internal_allowed`, `test_manage_internal_denied`, `test_manage_internal_transport_failure_returns_false`, `test_manage_internal_non_200_returns_false`, `test_manage_internal_no_token_returns_false_without_calling`, `test_manage_or_reservation_internal_closed_when_both_unreachable`)

**Out of scope.** Each other service's internal routes and their callers are in that
service's area. What reservations answers at `GET /internal/active` is
`reservations.md` (RES-INTERNAL-3).

### 8.16 First-startup superadmin

**What it does.** The very first account with full control is created from three
settings when the auth service starts.

**Surfaces.** Startup hook `services/auth/app/main.py`; settings `SUPERADMIN_EMAIL`,
`SUPERADMIN_USERNAME`, `SUPERADMIN_PASSWORD`.

**Rules.**

- **IAM-BOOT-1.** At startup, when all three settings are non-empty and no superadmin
  account exists, a local, active superadmin account is created from them (and joins
  "Not Grouped" like any new account); otherwise nothing happens. \
  Enforced in: `services/auth/app/main.py` (`_seed_superadmin`, `lifespan`) \
  Pinned by: `services/auth/tests/test_main.py` (`test_seed_superadmin_creates_new_superadmin`, `test_seed_superadmin_skips_when_env_vars_empty`, `test_seed_superadmin_skips_partial_env_vars`, `test_seed_superadmin_skips_when_already_exists`, `test_lifespan_runs_seeding`)
- **IAM-BOOT-2.** When `SUPERADMIN_EMAIL` already belongs to an account, no superadmin is
  created and a warning is logged. \
  Enforced in: `services/auth/app/main.py` (`_seed_superadmin`) \
  Pinned by: `services/auth/tests/test_main.py` (`test_seed_superadmin_skips_when_email_already_registered`)
- **IAM-BOOT-3.** The three values are trimmed of surrounding whitespace before use,
  the password included, and the password is not held to the registration length rules. \
  Enforced in: `services/auth/app/main.py` (`_seed_superadmin`) \
  Pinned by: none
- **IAM-BOOT-4.** At startup the "Not Grouped" group is created when no group of that
  name exists, tolerating a concurrent creator. \
  Enforced in: `services/auth/app/main.py` (`_seed_not_grouped`) \
  Pinned by: `services/auth/tests/test_main.py` (`test_seed_not_grouped_creates_default_group`, `test_seed_not_grouped_skips_when_exists`, `test_seed_not_grouped_handles_integrity_error`)
- **IAM-BOOT-5.** The seed runs in LDAP mode too; the account it creates is local and so
  cannot log in while `AUTH_METHOD=ldap` (IAM-LDAP-1). By decision; see the bootstrap
  note in [ADMIN_HANDBOOK.md](../ADMIN_HANDBOOK.md). \
  Enforced in: `services/auth/app/main.py` (`_seed_superadmin`) \
  Pinned by: none

**Out of scope.** The config service's own sign-in and the first-run settings page are
in `operations-and-observability.md`; login stays disabled until the config service
reports configured (IAM-UI-9).

### 8.17 Sign-in in the browser

**What it does.** The web app keeps the user signed in, renews tokens behind the scenes,
sends signed-out visitors to the login page, and keeps admin pages away from non-admins.

**Surfaces.** `frontend/src/stores/authStore.ts`, `frontend/src/api/client.ts`,
`frontend/src/api/auth.ts`, `frontend/src/components/guards.tsx`,
`frontend/src/routes.tsx`, `frontend/src/lib/roles.ts`,
`frontend/src/components/layout/AppLayout.tsx`.

**Rules.**

- **IAM-UI-1.** The access and refresh tokens live in `localStorage`; the app counts as
  signed in whenever an access token is stored. \
  Enforced in: `frontend/src/stores/authStore.ts` (`useAuthStore`) \
  Pinned by: `frontend/src/test/stores/authStore.test.ts` (`setTokens persists to localStorage and updates state`, `reads tokens from localStorage on initialization`, `clearAuth removes tokens and resets state`)
- **IAM-UI-2.** A 401 on any call other than login and refresh triggers one refresh;
  concurrent 401s wait for that single refresh and are retried with the new token, and
  each request is retried at most once. \
  Enforced in: `frontend/src/api/client.ts` (`apiClient`, `processQueue`) \
  Pinned by: `frontend/src/test/api/client.test.ts` (`refreshes token and retries original request on 401`, `queues concurrent 401s behind a single refresh and retries all of them`, `does not loop refresh when the refresh endpoint itself returns 401`, `does not attempt refresh for non-401 errors`)
- **IAM-UI-3.** A failed refresh, or a 401 with no stored refresh token, clears the
  stored tokens and rejects every waiting request. \
  Enforced in: `frontend/src/api/client.ts` (`apiClient`) \
  Pinned by: `frontend/src/test/api/client.test.ts` (`clears auth when 401 received with no refresh token`, `clears auth when refresh request itself fails`, `rejects queued requests when the shared refresh fails`)
- **IAM-UI-4.** Logout clears the stored tokens and cached data even when the server
  call fails. \
  Enforced in: `frontend/src/api/auth.ts` (`useLogout`) \
  Pinned by: `frontend/src/test/api/auth.test.tsx` (`useLogout clears auth even when the server errors`); `tests/e2e/test_register_and_roles.py` (`test_logout_returns_to_login_and_clears_tokens`)
- **IAM-UI-5.** `AuthGuard` sends a signed-out visitor to `/login`; `GuestGuard` sends a
  signed-in visitor away from `/login` and `/register` to `/topology`. \
  Enforced in: `frontend/src/components/guards.tsx` (`AuthGuard`, `GuestGuard`) \
  Pinned by: `frontend/src/test/components/guards.test.tsx` (`redirects an unauthenticated user to /login`, `redirects an authenticated user to /topology`, `renders children for an authenticated user`, `renders children for an unauthenticated user`)
- **IAM-UI-6.** `AdminGuard` renders nothing until the account is loaded, then sends a
  non-admin to `/topology`. It is a convenience: the backend gates every admin route on
  its own. \
  Enforced in: `frontend/src/components/guards.tsx` (`AdminGuard`) \
  Pinned by: `frontend/src/test/components/AdminGuard.test.tsx` (`redirects a non-admin user to /topology and renders nothing`, `renders children for an admin user`, `renders children for a superadmin user`, `renders nothing without crashing while unauthenticated`)
- **IAM-UI-7.** Exactly sixteen paths sit behind `AdminGuard` (the fifteen `/admin/*`
  pages and `/reporting`), all inside `AuthGuard`. \
  Enforced in: `frontend/src/routes.tsx` (`appRouteElements`) \
  Pinned by: `frontend/src/test/routes.test.tsx` (`the set of AdminGuard-guarded paths equals the expected list exactly`, `every AdminGuard-guarded path also sits under AuthGuard`, `the only unguarded /admin-prefixed path is the bare /admin redirect`); `tests/e2e/test_admin_guard_redirect_playwright.py` (`test_non_admin_redirected_from_remaining_admin_guarded_paths`)
- **IAM-UI-8.** `isAdminRole` is true only for the exact strings `admin` and
  `superadmin`, and is read from the stored role `GET /me` reports (IAM-ROLE-8). \
  Enforced in: `frontend/src/lib/roles.ts` (`isAdminRole`); `frontend/src/components/layout/AppLayout.tsx` (`AppLayout`) \
  Pinned by: `frontend/src/test/lib/roles.test.ts` (`returns true for admin`, `returns true for superadmin`, `returns false for user`, `is case-sensitive: Admin does not count`); `tests/e2e/test_register_and_roles.py` (`test_non_admin_has_no_administration_menu`)
- **IAM-UI-9.** The login form is disabled and refuses to submit while the config
  service reports the system unconfigured; any login failure shows one message, `Invalid
  email or password`. \
  Enforced in: `frontend/src/pages/LoginPage.tsx` (`LoginPage`) \
  Pinned by: `frontend/src/test/pages/LoginPage.test.tsx` (`shows the unconfigured banner and disables fields when configured=false`, `toasts an error when login is rejected`)
- **IAM-UI-10.** Registration success sends the user to `/login`; a failure shows the
  server's detail. \
  Enforced in: `frontend/src/pages/RegisterPage.tsx` (`RegisterPage`) \
  Pinned by: `frontend/src/test/pages/RegisterPage.test.tsx` (`navigates to /login and toasts on success`, `toasts the backend detail on failure`)

**Out of scope.** Theming and preferences (`operations-and-observability.md`).

## 9. Errors

FastAPI validation errors (422) carry `detail` as a list of `{loc, msg, type}`; every
other error carries `detail` as a string. A 401 from a bearer check also sends
`WWW-Authenticate: Bearer`.

| Status | Error key or detail | When | Rule |
|---|---|---|---|
| 400 | `Cannot assign the superadmin role via the API` | role change to superadmin | IAM-USER-3 |
| 400 | `Cannot change your own role` | role change on oneself | IAM-USER-4 |
| 400 | `Cannot change the superadmin's role` | role change on a superadmin | IAM-USER-5 |
| 400 | `Cannot deactivate the superadmin` | deactivate a superadmin | IAM-ACCT-1 |
| 400 | `Token role cannot exceed the principal's role` | token role above its principal | IAM-APITOK-3 |
| 401 | `Not authenticated` (FastAPI) | no Authorization header on a JWT-guarded route, auth or acl | IAM-JWT-3, IAM-CLAIM-1 |
| 401 | `Could not validate credentials` | bad, expired, or subjectless token; in auth also an unknown or inactive account | IAM-JWT-3, IAM-ACCT-5, IAM-CLAIM-1 |
| 401 | `Invalid credentials` | any login refusal, local or LDAP | IAM-LOGIN-1, IAM-LDAP-5, IAM-LDAP-9, IAM-LDAP-10 |
| 401 | `Invalid or expired refresh token` | refresh refused | IAM-SESSION-2 |
| 401 | `Invalid or expired token` | API token exchange refused | IAM-APITOK-8 |
| 401 | `Invalid subject in token` | another service's `caller_id` on a non-UUID `sub` | IAM-CLAIM-3 |
| 403 | `You do not have permission to perform this action` | an auth role gate | IAM-ROLE-5 |
| 403 | `Cannot mint a token whose role or principal exceeds your own role` | token rank check | IAM-APITOK-2 |
| 403 | `Admin or superadmin role required` | an acl grant route as a non-admin claim | IAM-ACL-4, IAM-CLAIM-2 |
| 403 | `Cannot query permissions for another user` | acl check, batch, or resources about someone else | IAM-ACL-8 |
| 403 | `Invalid internal token` | an internal route with a wrong token | IAM-INTERNAL-2 |
| 404 | `User not found` | role change, activate, deactivate, add member, or an internal lookup on an unknown account; internal groups on an inactive one | IAM-USER-5, IAM-ACCT-1, IAM-ACCT-2, IAM-GROUP-6, IAM-INTERNAL-4, IAM-INTERNAL-5 |
| 404 | `Principal user not found` | token create for an unknown principal | IAM-APITOK-1 |
| 404 | `Group not found` | any group route on an unknown group (admin caller) | IAM-GROUP-6, IAM-GROUP-7, IAM-GROUP-8, IAM-GROUP-9 |
| 404 | `Member not found` | remove a non-member | IAM-GROUP-7 |
| 404 | `HERD group not found` | mapping create for an unknown or vanished group | IAM-MAP-1, IAM-MAP-6 |
| 404 | `Mapping not found` | delete an unknown mapping | IAM-MAP-8 |
| 404 | `Sync run not found` | read an unknown run | IAM-LOOP-8 |
| 404 | `Grant not found` | read or delete an unknown grant | IAM-ACL-4 |
| 409 | `Local registration is disabled; this deployment uses LDAP authentication.` | register in LDAP mode | IAM-REG-1 |
| 409 | `Email or username already exists` | register collision | IAM-REG-3 |
| 409 | `Cannot deactivate your own account` | deactivate oneself | IAM-ACCT-1 |
| 409 | `A group with this name already exists` | group create or rename collision | IAM-GROUP-3 |
| 409 | `User is already a member of this group` | add an existing member | IAM-GROUP-6 |
| 409 | `Directory mappings require auth_method=ldap` | mapping create outside LDAP mode | IAM-MAP-1 |
| 409 | `A mapping for this group_dn already exists`, `This HERD group already has a directory mapping`, `Mapping conflicts with concurrent changes; retry` | mapping conflicts | IAM-MAP-2, IAM-MAP-6 |
| 409 | `Directory sync requires auth_method=ldap` | sync-now outside LDAP mode | IAM-SERIAL-2 |
| 409 | `A sync run is already in progress`, `A sync run is already in progress on another replica` | sync-now while a run holds the slot | IAM-SERIAL-1 |
| 409 | `This grant already exists` | duplicate grant | IAM-ACL-2 |
| 422 | validation list | registration, login, group, bulk, mapping, token, grant, or check body out of bounds; unknown resource type or permission; missing internal-token header | IAM-REG-2, IAM-LOGIN-3, IAM-GROUP-3, IAM-GROUP-8, IAM-GROUP-9, IAM-APITOK-5, IAM-ACL-1, IAM-ACL-10, IAM-INTERNAL-2 |
| 422 | `group_dn does not resolve in the directory` | mapping DN proven absent | IAM-MAP-3 |
| 500 | no structured body | bulk add naming an id with no account | IAM-GROUP-13 |
| 503 | `Directory unavailable, mapping not validated: <directory error>` | mapping create while the directory cannot be asked | IAM-MAP-3 |
| 503 | `Internal API token not configured` | an internal route with no token configured | IAM-INTERNAL-2 |

Codes that are success but do nothing: logout with an unknown refresh token answers
204 (IAM-SESSION-3); revoking an unknown API token answers 204 (IAM-APITOK-7); setting
the current role answers 200 (IAM-USER-6); activating an active account answers 200
(IAM-ACCT-2).

## 10. Interactions with other services

Calls into this area are in section 7 and the forwarded-JWT group routes noted under it.

| Direction | Peer | Call | Purpose | On failure |
|---|---|---|---|---|
| Out (auth) | LDAP directory | search and bind at login | password check, identity | Fail closed: 401 (IAM-LDAP-5) |
| Out (auth) | LDAP directory | base search of the group DN | mapping validation | Fail closed: 503 when it cannot be asked, 422 when it proves absence (IAM-MAP-3) |
| Out (auth) | LDAP directory | group fetch, member resolution, paged email enumeration | sync and sweep | Fail closed per group (IAM-SYNC-2) and for the whole sweep (IAM-SWEEP-5); the run ends `partial` |
| Out (acl) | auth | `GET /groups/user/{user_id}` (caller's JWT, 10 s) | group set for a check | Fail closed: no groups, so not allowed (IAM-ACL-9) |
| Out (acl) | auth | `GET /internal/users/{user_id}/groups` (internal token, 10 s) | group set for the internal check | Fail closed: not allowed (IAM-ACL-12) |
| Out (`herd_common`) | acl | `POST /check` (caller's JWT, 5 s) | explicit grant | Fail closed: false (IAM-HELPER-1) |
| Out (`herd_common`) | acl | `POST /internal/check` (internal token, 5 s) | explicit grant without a user token | Fail closed: false (IAM-HELPER-4) |
| Out (`herd_common`) | reservations | `GET /internal/active` (internal token, 5 s) | reservation-owner pass | Fail closed: false (IAM-HELPER-3) |

## 11. Configuration

[ENV_VARS.md](../ENV_VARS.md) has the full list. Names are the compose variables.

| Setting | Default | Effect |
|---|---|---|
| `AUTH_SECRET_KEY` | none (required) | JWT signing key, shared by every JWT-checking service |
| `AUTH_ALGORITHM` | `HS256` | JWT algorithm |
| `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | Access token lifetime, also the exchange's `expires_in` |
| `AUTH_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifetime |
| `INTERNAL_API_TOKEN` | empty | Shared service secret; empty makes auth's and acl's internal routes answer 503 and the shared helpers answer false |
| `SUPERADMIN_EMAIL`, `SUPERADMIN_USERNAME`, `SUPERADMIN_PASSWORD` | empty | First-startup superadmin; any empty skips the seed |
| `AUTH_METHOD` | `local` | `local` or `ldap`; one backend for every login |
| `LDAP_SERVER_URL`, `LDAP_BIND_DN`, `LDAP_BIND_PASSWORD`, `LDAP_USER_BASE_DN` | empty | Directory location and service account |
| `LDAP_USER_FILTER` | `(sAMAccountName={username})` | Login search filter |
| `LDAP_EMAIL_ATTRIBUTE`, `LDAP_USERNAME_ATTRIBUTE` | `mail`, `sAMAccountName` | Identity attributes |
| `LDAP_USE_TLS`, `LDAP_TLS_VALIDATE`, `LDAP_CA_CERT` | `true`, `true`, empty | TLS and certificate checking |
| `LDAP_GROUP_MEMBER_ATTRIBUTE`, `LDAP_GROUP_NAME_ATTRIBUTE` | `member`, `cn` | Group entry attributes |
| `LDAP_GROUP_SYNC_ENABLED` | `false` | Starts the interval loop (LDAP mode only) |
| `LDAP_SYNC_INTERVAL_SECONDS` | `3600` | Loop interval; raised to 60 when lower |
| `LDAP_SYNC_RUNS_RETENTION_DAYS` | `90` | Run-row retention, pruned by the loop only |
| `LDAP_SYNC_RUN_STALE_SECONDS` | `7200` | Reaper threshold, floored per IAM-REAP-2 |
| `LDAP_SYNC_DEACTIVATION_ENABLED` | `false` | Turns the sweep on |
| `LDAP_DISABLED_FILTER` | empty | Disabled-account filter; empty means absence only |
| `LDAP_SYNC_DEACTIVATION_MAX_PERCENT`, `LDAP_SYNC_DEACTIVATION_MIN_COUNT` | `20`, `3` | Breaker terms |

Fixed in code: LDAP connect timeout 10 s and receive timeout 30 s on the service
connection (the login user bind sets no receive timeout); paged search size 500; detail
cap 20 per category; prune cadence 24 hours; bulk and batch caps 500.

## 12. Test coverage map

| Level | Where | Notes |
|---|---|---|
| Unit | `services/auth/tests/` (in-memory SQLite, `test_effective_role_unit.py`, `test_api_tokens.py`, `test_ldap_service.py`, `test_ldap_sync_service.py`, `test_ldap_sync_service_scaling.py`, `test_ldap_sync_stale_run_reaper.py`, `test_ldap_sync_loop.py`); `services/acl/tests/test_grant_service_unit.py`, `test_auth_client.py`, `test_schema_validators.py`; `services/common/tests/test_auth.py`, `test_internal_auth.py`, `test_acl.py`; `frontend/src/test/` (`api/client.test.ts`, `stores/authStore.test.ts`, `lib/roles.test.ts`, `components/guards.test.tsx`, `components/AdminGuard.test.tsx`, `routes.test.tsx`, the admin page tests) | SQLite has no advisory locks, so only the in-process half of the run slot is exercised here |
| Functional (through the service API) | `services/auth/tests/test_auth.py`, `test_groups.py`, `test_internal.py`, `test_ldap_sync.py`, `test_effective_role_http.py`, `test_auth_ldap.py`; `services/acl/tests/test_grants.py`, `test_internal.py`; the live suites `services/auth/tests/test_ldap_service_live.py` (real directory) and `services/auth/tests/test_ldap_sync_service_live_pg.py` (real Postgres) | The live-LDAP suite runs in the gates' live-LDAP phase; the Postgres suite in `_gate-pg-live-tests` |
| Integration (running stack) | `tests/integration/test_auth_flow.py`, `test_acl_flow.py`, `test_ldap_auth.py`, `test_ldap_sync_admin.py` | The LDAP ones run only in the gates' LDAP-mode phase |
| Stress and load | `tests/load/locustfile.py` (every user logs in; `ACLChecker` exercises the check route) | No load test covers refresh, the token exchange, or a sync run |
| Browser end-to-end | `tests/e2e/test_login.py`, `test_register_and_roles.py`, `test_roles_playwright.py`, `test_admin_guard_redirect_playwright.py`, `test_user_groups_playwright.py`, `test_acl_grants_playwright.py`, `test_ldap_sync_admin_playwright.py`, `test_ldap_login.py` | Runs nightly and in the gates; the LDAP login tests are exempt skips on the local-mode gate stack |

Not run for this document: only the drift guard and the repository-root unit suite were
run. The auth, acl, and common service suites, the live-LDAP and live-Postgres suites,
the integration suite, the frontend tests, and the browser suite were read, not run.

## 13. Known limits and gaps

### Open defects

- #1009 (IAM-GROUP-13, IAM-MAP-3): a bulk member add with an unknown user id fails the
  whole request with an unhandled error (500), while a single add answers 404. The
  mapping-create 503 detail carries the directory exception text.

### Limits by decision

- An admin may mint an API token for any principal of equal or lower rank, including
  another admin, and then act as that account (IAM-APITOK-2). Recorded in issue #312 and
  the comment in `create_token`; [SECURITY.md](../../SECURITY.md) treats admins as
  trusted.
- Other services trust the role claim until the token expires, so a demotion or a
  deactivation binds there only after at most `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES`
  (IAM-CLAIM-2, IAM-ACCT-5). Recorded in [SECURITY.md](../../SECURITY.md) (Threat model,
  JWT), [ROLES.md](../ROLES.md), and ADR 0011 (Deactivation sweep: login and refresh are
  the only enforcement points).
- Any signed-in account may read any user's group memberships (IAM-GROUP-10). Recorded
  in the API Reference by Role table of [ROLES.md](../ROLES.md).
- Internal routes are reachable through the gateway by anyone who holds the internal
  token. Recorded in [SECURITY.md](../../SECURITY.md) (Threat model).
- LDAP login refuses a username collision while sync recovers it (IAM-LDAP-10,
  IAM-SYNC-7). Recorded in the comments of `_authenticate_ldap` and `_ensure_ldap_user`.
- The seeded superadmin is local and cannot log in under `AUTH_METHOD=ldap`
  (IAM-BOOT-5). Recorded in [ADMIN_HANDBOOK.md](../ADMIN_HANDBOOK.md) (bootstrap note).
- A mapping whose directory group is renamed or moved dangles until re-created
  (IAM-SYNC-2). Recorded in ADR 0011 (Mapping store).
- Sync-now never prunes run rows (IAM-LOOP-6). Recorded in the docstring of
  `ldap_sync_loop.py` and ADR 0011 (Audit).
- The superadmin can be activated by any admin, with no carve-out (IAM-ACCT-2).
  Recorded in the docstring of `deactivate_user` and [ROLES.md](../ROLES.md).
- The deactivation breaker's denominator is every LDAP account, inactive ones included
  (IAM-SWEEP-7). Recorded in ADR 0011 (as amended 2026-08-12: one pass over all LDAP
  users).
- Three documents disagree with the rules here, tracked as issue #1010: the handbook's
  superadmin promotion (IAM-USER-3), the FEATURES.md statement that superadmin accounts
  remain local (IAM-LDAP-1, IAM-USER-9), and the SECURITY.md statement that rotating the
  secret invalidates every live session (IAM-JWT-1, IAM-SESSION-1).

### Rules with no test

- IAM-SESSION-1: refresh token storage as a hash and its lifetime.
- IAM-LOGIN-2: the dummy-hash timing equalization (only the dummy hash's validity is tested).
- IAM-LOGIN-3: the login identifier and password bounds.
- IAM-LDAP-11: the username fallback to the typed login name.
- IAM-JWT-1: the claim set and lifetime of a login access token.
- IAM-JWT-2: a refresh reissuing the current database role.
- IAM-ROLE-8: `GET /me` reporting the stored role.
- IAM-APITOK-5: the token name and expiry bounds.
- IAM-GROUP-5: membership and mapping removal on group delete (the test checks only that the group is gone).
- IAM-GROUP-11: "Not Grouped" found by name.
- IAM-GROUP-13: a bulk add with a repeated or unknown id.
- IAM-SYNC-3: the display-name refresh during a sync.
- IAM-SYNC-18: sync never changes a role.
- IAM-SERIAL-3: the connection invalidation after a failed unlock.
- IAM-LOOP-6: sync-now never prunes.
- IAM-BOOT-3: trimming of the seed values.
- IAM-BOOT-5: the seed in LDAP mode.
- IAM-USER-9: a superadmin session in LDAP mode only from an earlier refresh token or an API token.
