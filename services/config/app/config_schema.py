CONFIG_SCHEMA = [
    # Database
    {
        "key": "POSTGRES_USER",
        "label": "Database User",
        "type": "string",
        "required": True,
        "group": "Database",
        "secret": False,
        "description": "PostgreSQL username",
    },
    {
        "key": "POSTGRES_PASSWORD",
        "label": "Database Password",
        "type": "password",
        "required": True,
        "group": "Database",
        "secret": True,
        "description": "PostgreSQL password",
    },
    {
        "key": "POSTGRES_DB",
        "label": "Database Name",
        "type": "string",
        "required": True,
        "group": "Database",
        "secret": False,
        "description": "PostgreSQL database name",
    },
    # Authentication
    {
        "key": "AUTH_SECRET_KEY",
        "label": "JWT Secret Key",
        "type": "password",
        "required": True,
        "group": "Authentication",
        "secret": True,
        "description": "Secret key used to sign JWT tokens; change invalidates all sessions",
    },
    {
        "key": "AUTH_ALGORITHM",
        "label": "JWT Algorithm",
        "type": "dropdown",
        "required": False,
        "group": "Authentication",
        "secret": False,
        "default": "HS256",
        "options": ["HS256", "HS384", "HS512"],
        "description": "Algorithm used to sign JWT tokens",
    },
    {
        "key": "AUTH_ACCESS_TOKEN_EXPIRE_MINUTES",
        "label": "Access Token Expiry (minutes)",
        "type": "number",
        "required": False,
        "group": "Authentication",
        "secret": False,
        "default": 30,
        "description": "Minutes before an access token expires",
    },
    {
        "key": "AUTH_REFRESH_TOKEN_EXPIRE_DAYS",
        "label": "Refresh Token Expiry (days)",
        "type": "number",
        "required": False,
        "group": "Authentication",
        "secret": False,
        "default": 7,
        "description": "Days before a refresh token expires",
    },
    {
        "key": "AUTH_METHOD",
        "label": "Authentication Method",
        "type": "dropdown",
        "required": False,
        "group": "Authentication",
        "secret": False,
        "default": "local",
        "options": ["local", "ldap"],
        "description": (
            "Where user credentials are checked: 'local' uses bcrypt-hashed "
            "passwords in the HERD database; 'ldap' binds against the configured "
            "directory server. Only one method is active at a time."
        ),
    },
    # LDAP / Active Directory (used when AUTH_METHOD=ldap)
    {
        "key": "LDAP_SERVER_URL",
        "label": "LDAP Server URL",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "description": (
            "Directory server URL, e.g. ldaps://ad.example.com:636 or ldap://ad.example.com:389"
        ),
    },
    {
        "key": "LDAP_BIND_DN",
        "label": "Service Account Bind DN",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "description": (
            "DN used to bind for user lookups, "
            "e.g. CN=herd-svc,OU=ServiceAccounts,DC=example,DC=com. "
            "Leave blank for anonymous search."
        ),
    },
    {
        "key": "LDAP_BIND_PASSWORD",
        "label": "Service Account Password",
        "type": "password",
        "required": False,
        "group": "LDAP",
        "secret": True,
        "description": "Password for the service account used to search the directory",
    },
    {
        "key": "LDAP_USER_BASE_DN",
        "label": "User Search Base DN",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "description": "Base DN to search under for users, e.g. OU=Users,DC=example,DC=com",
    },
    {
        "key": "LDAP_USER_FILTER",
        "label": "User Search Filter",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "(sAMAccountName={username})",
        "description": (
            "LDAP filter used to find a user by login name. "
            "'{username}' is substituted with the escaped login input."
        ),
    },
    {
        "key": "LDAP_EMAIL_ATTRIBUTE",
        "label": "Email Attribute",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "mail",
        "description": "Directory attribute that holds the user's email address",
    },
    {
        "key": "LDAP_USERNAME_ATTRIBUTE",
        "label": "Username Attribute",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "sAMAccountName",
        "description": "Directory attribute used as the HERD username",
    },
    {
        "key": "LDAP_USE_TLS",
        "label": "Use TLS",
        "type": "dropdown",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "true",
        "options": ["true", "false"],
        "description": (
            "Require TLS for the LDAP connection. ldaps:// URLs negotiate TLS "
            "implicitly; plain ldap:// URLs use STARTTLS when this is true."
        ),
    },
    {
        "key": "LDAP_TLS_VALIDATE",
        "label": "Validate TLS Certificate",
        "type": "dropdown",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "true",
        "options": ["true", "false"],
        "description": (
            "Verify the directory server's TLS certificate. Leave true: the bind "
            "transmits the service-account and every user's password, so an "
            "unvalidated certificate lets an active network attacker MITM the "
            "connection. Set false only for a lab directory behind a self-signed "
            "cert you cannot pin via LDAP_CA_CERT; this logs a startup warning."
        ),
    },
    {
        "key": "LDAP_CA_CERT",
        "label": "CA Certificate Path",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "description": (
            "Path (inside the container) to a CA bundle to verify the directory "
            "server against, e.g. a pinned internal CA. Used when "
            "LDAP_TLS_VALIDATE=true; when blank the system trust store is used."
        ),
    },
    {
        "key": "LDAP_GROUP_MEMBER_ATTRIBUTE",
        "label": "Group Member Attribute",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "member",
        "description": (
            "Group entry attribute holding member DNs (Active Directory and "
            "groupOfNames use 'member'; posixGroup memberUid is out of scope). "
            "Consulted by the ADR 0011 group sync."
        ),
    },
    {
        "key": "LDAP_GROUP_NAME_ATTRIBUTE",
        "label": "Group Name Attribute",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "cn",
        "description": (
            "Group entry attribute cached as a mapping's display name. "
            "Consulted by the ADR 0011 group sync."
        ),
    },
    {
        "key": "LDAP_GROUP_SYNC_ENABLED",
        "label": "Enable Group Sync Interval Loop",
        "type": "dropdown",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "false",
        "options": ["true", "false"],
        "description": (
            "Start the background loop that runs the directory group reconcile "
            "on an interval (ADR 0011 phase 5). Also requires AUTH_METHOD=ldap; "
            "dark by default: admin-managed mappings and on-demand sync-now work "
            "regardless of this setting."
        ),
    },
    {
        "key": "LDAP_SYNC_INTERVAL_SECONDS",
        "label": "Group Sync Interval (seconds)",
        "type": "number",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": 3600,
        "description": (
            "Seconds between interval-triggered LDAP group reconcile runs. "
            "Values below 60 are clamped to a 60-second floor (with a startup "
            "warning) rather than rejected, so a bad tuning value never blocks "
            "auth from booting."
        ),
    },
    {
        "key": "LDAP_SYNC_RUNS_RETENTION_DAYS",
        "label": "Sync Run Retention (days)",
        "type": "number",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": 90,
        "description": (
            "Days to keep ldap_sync_runs audit rows before the interval loop "
            "prunes them. A running row is never pruned regardless of age."
        ),
    },
    {
        "key": "LDAP_SYNC_RUN_STALE_SECONDS",
        "label": "Stale Run Threshold (seconds)",
        "type": "number",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": 7200,
        "description": (
            "Seconds an ldap_sync_runs row may sit at status running before the "
            "stale-run reaper flips it to failed, the backstop for a hard process "
            "death mid-run. Floored at the point of use (with a warning, never a "
            "boot failure) to the larger of 60 seconds and TWICE the effective "
            "sync interval, so a value at or below the interval cannot fail a run "
            "that is merely slow."
        ),
    },
    {
        "key": "LDAP_SYNC_DEACTIVATION_ENABLED",
        "label": "Enable Deactivation Sweep",
        "type": "dropdown",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": "false",
        "options": ["true", "false"],
        "description": (
            "Opt-in for the deactivation/reactivation sweep. Independent of group "
            "mirroring: enabling mappings alone never deactivates anyone."
        ),
    },
    {
        "key": "LDAP_DISABLED_FILTER",
        "label": "Disabled Account Filter",
        "type": "string",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "description": (
            "A complete LDAP filter expression (not a value) identifying disabled "
            "accounts, e.g. the AD userAccountControl lockout-bit filter "
            "(userAccountControl:1.2.840.113556.1.4.803:=2). Blank means "
            "absence-only detection."
        ),
    },
    {
        "key": "LDAP_SYNC_DEACTIVATION_MAX_PERCENT",
        "label": "Deactivation Breaker: Max Percent",
        "type": "number",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": 20,
        "description": (
            "Circuit-breaker percent term: the sweep aborts, deactivating no one, "
            "only when the proven-absent-or-disabled count STRICTLY exceeds this "
            "percent of swept users AND the count floor below. Reactivations "
            "still apply on abort."
        ),
    },
    {
        "key": "LDAP_SYNC_DEACTIVATION_MIN_COUNT",
        "label": "Deactivation Breaker: Min Count",
        "type": "number",
        "required": False,
        "group": "LDAP",
        "secret": False,
        "default": 3,
        "description": (
            "Circuit-breaker count floor, strictly exceeded together with the "
            "percent term. Keeps small deployments functional: one leaver in a "
            "four-user shop is 25 percent but under the floor, so it deactivates."
        ),
    },
    # Superadmin
    {
        "key": "SUPERADMIN_EMAIL",
        "label": "Superadmin Email",
        "type": "string",
        "required": False,
        "group": "Superadmin",
        "secret": False,
        "description": "Email for the initial superadmin account (only used on first startup)",
    },
    {
        "key": "SUPERADMIN_USERNAME",
        "label": "Superadmin Username",
        "type": "string",
        "required": False,
        "group": "Superadmin",
        "secret": False,
        "description": "Username for the initial superadmin account",
    },
    {
        "key": "SUPERADMIN_PASSWORD",
        "label": "Superadmin Password",
        "type": "password",
        "required": False,
        "group": "Superadmin",
        "secret": True,
        "description": "Password for the initial superadmin account",
    },
    # Security
    {
        "key": "INTERNAL_API_TOKEN",
        "label": "Internal API Token",
        "type": "password",
        "required": True,
        "group": "Security",
        "secret": True,
        "description": "Token used for service-to-service authentication",
    },
    # CORS
    {
        "key": "CORS_ORIGINS",
        "label": "CORS Origins",
        "type": "string",
        "required": False,
        "group": "CORS",
        "secret": False,
        "default": "https://localhost",
        "description": "Comma-separated list of allowed origins",
    },
    # Messaging
    {
        "key": "NATS_URL",
        "label": "NATS URL",
        "type": "string",
        "required": False,
        "group": "Messaging",
        "secret": False,
        "default": "nats://nats:4222",
        "description": "NATS JetStream connection URL",
    },
    # AI Integration
    {
        "key": "AI_PROVIDER",
        "label": "AI Provider",
        "type": "dropdown",
        "required": False,
        "group": "AI Integration",
        "secret": False,
        "default": "anthropic",
        "options": ["anthropic", "openai_compat"],
        "description": (
            "Which LLM backend to use. 'anthropic' calls the Anthropic API directly. "
            "'openai_compat' speaks the OpenAI chat-completions wire format and works "
            "against vLLM, Ollama, LM Studio, OpenAI proper, Azure OpenAI, or any "
            "compatible gateway; set AI_BASE_URL to the endpoint."
        ),
    },
    {
        "key": "AI_BASE_URL",
        "label": "AI Base URL",
        "type": "string",
        "required": False,
        "group": "AI Integration",
        "secret": False,
        "description": (
            "Endpoint URL when AI_PROVIDER=openai_compat, e.g. "
            "http://vllm:8000/v1 or http://ollama:11434/v1. Ignored when "
            "AI_PROVIDER=anthropic."
        ),
    },
    {
        "key": "AI_API_KEY",
        "label": "AI API Key",
        "type": "password",
        "required": False,
        "group": "AI Integration",
        "secret": True,
        "description": (
            "API key for the configured AI provider. Local servers (vLLM, Ollama, "
            "LM Studio) typically accept any non-empty placeholder. Blank disables "
            "the AI features when AI_PROVIDER=anthropic."
        ),
    },
    {
        "key": "AI_MODEL",
        "label": "AI Model",
        "type": "string",
        "required": False,
        "group": "AI Integration",
        "secret": False,
        "default": "claude-sonnet-4-6",
        "description": (
            "Model identifier passed to the provider. Format is provider-specific: "
            "claude-* for anthropic; provider-and-deployment-specific for openai_compat "
            "(e.g. Qwen/Qwen3-35B-Instruct on vLLM, gpt-4o-mini on OpenAI proper)."
        ),
    },
    {
        "key": "ANTHROPIC_API_KEY",
        "label": "Anthropic API Key (deprecated)",
        "type": "password",
        "required": False,
        "group": "AI Integration",
        "secret": True,
        "description": (
            "Deprecated. Not honored as a fallback for AI_API_KEY; setting this "
            "alone does not enable AI features. Use AI_API_KEY instead. Leaving "
            "this set while AI_API_KEY is blank logs a startup warning from "
            "ai-orchestrator."
        ),
    },
    # Logging
    {
        "key": "LOG_LEVEL",
        "label": "Log Level",
        "type": "dropdown",
        "required": False,
        "group": "Logging",
        "secret": False,
        "default": "INFO",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "description": "Log verbosity for all backend services",
    },
]

REQUIRED_KEYS = [f["key"] for f in CONFIG_SCHEMA if f["required"]]

GROUPS = list(dict.fromkeys(f["group"] for f in CONFIG_SCHEMA))
