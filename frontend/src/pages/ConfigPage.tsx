import { useState, useEffect, useMemo, useRef } from "react";
import { Link } from "react-router-dom";
import toast from "react-hot-toast";
import { TOAST_CLEARANCE_CLASS } from "@/lib/toastClearance";
import {
  configChangePasswordErrorText,
  errorDetail,
  loginRetryAfterSeconds,
  loginRetryAfterText,
  structuredDetail,
} from "@/lib/errors";
import { ArrowLeft, Eye, EyeOff, ChevronDown, ChevronRight } from "lucide-react";
import { AuthorCredit } from "@/components/layout/AuthorCredit";
import { useConfigStore } from "@/stores/configStore";
import { configPasswordLengthOk } from "@/lib/configPassword";
import {
  answerWasLost,
  waitForRestart,
  RESTART_FOLLOW_LOST_TEXT,
  RESTART_NOT_STARTED_TEXT,
  RESTART_TIMED_OUT_TEXT,
  RESTART_UNREADABLE_TEXT,
  type RestartOutcome,
} from "@/lib/configRestart";
import {
  fetchLastRestart,
  useConfigLogin,
  useChangeConfigPassword,
  useConfigSchema,
  useConfigSettings,
  useSaveConfigSettings,
  useApplyConfig,
  type ConfigField,
  type LastRestart,
  type RestartResult,
} from "@/api/config";

// -- Config Login --

// setTimeout takes a signed 32-bit delay; a longer one fires at once.
const MAX_TIMER_MS = 2_147_483_647;

function ConfigLogin({
  onLoginSuccess,
}: {
  onLoginSuccess: (passwordChanged: boolean) => void;
}) {
  const [password, setPassword] = useState("");
  const login = useConfigLogin();
  // Issue #1126: a 429 from the attempt limit (OPS-CONFIG-22) holds Sign in
  // for the Retry-After wait, since an attempt inside it is refused before
  // the password is even checked. A fresh object per refusal re-arms the
  // timer below.
  const [lockout, setLockout] = useState<{ seconds: number } | null>(null);

  useEffect(() => {
    if (!lockout) return;
    const handle = setTimeout(
      () => setLockout(null),
      Math.min(lockout.seconds * 1000, MAX_TIMER_MS),
    );
    return () => clearTimeout(handle);
  }, [lockout]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (lockout) return;
    try {
      const result = await login.mutateAsync(password);
      onLoginSuccess(result.password_changed);
    } catch (err) {
      const lockedText = loginRetryAfterText(err);
      if (lockedText === null) {
        toast.error("Invalid config password");
        return;
      }
      toast.error(lockedText);
      const seconds = loginRetryAfterSeconds(err);
      if (seconds !== null) setLockout({ seconds });
    }
  };

  return (
    <div className="w-full max-w-sm bg-white rounded-xl shadow-sm border border-gray-200 p-8">
      <div className="mb-6 text-center">
        <h1 className="text-2xl font-bold text-gray-900">HERD Configuration</h1>
        <p className="text-sm text-gray-500 mt-1">
          Enter the config password to continue
        </p>
      </div>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label
            htmlFor="config-password"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Config Password
          </label>
          <input
            id="config-password"
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="password"
          />
        </div>
        <button
          type="submit"
          disabled={login.isPending || lockout !== null}
          className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {login.isPending ? "Authenticating..." : "Sign in"}
        </button>
      </form>
      <p className="text-center text-sm text-gray-500 mt-4">
        <Link to="/login" className="text-blue-600 hover:underline font-medium">
          Back to login
        </Link>
      </p>
    </div>
  );
}

// -- Password Change --

function PasswordChange({ onChanged }: { onChanged: () => void }) {
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const changePassword = useChangeConfigPassword();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPassword !== confirmPassword) {
      toast.error("Passwords do not match");
      return;
    }
    // Characters are code points, as the server counts them (issue #1452).
    if (!configPasswordLengthOk(newPassword)) {
      toast.error("Password must be 8 to 32 characters");
      return;
    }
    try {
      await changePassword.mutateAsync(newPassword);
      toast.success("Config password changed");
      onChanged();
    } catch (err) {
      // The server's own sentence for a refused password, else the generic text.
      toast.error(configChangePasswordErrorText(err));
    }
  };

  return (
    <div className="w-full max-w-sm bg-white rounded-xl shadow-sm border border-gray-200 p-8">
      <div className="mb-6 text-center">
        <h1 className="text-2xl font-bold text-gray-900">Change Config Password</h1>
        <p className="text-sm text-gray-500 mt-1">
          You must change the default password before configuring HERD
        </p>
      </div>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label
            htmlFor="new-password"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            New Password (8-32 characters)
          </label>
          {/* No minLength or maxLength on either input: they count UTF-16 units, and
              maxLength cuts a pasted value, so the bound is configPasswordLengthOk's
              code point count alone (issue #1452). */}
          <input
            id="new-password"
            type="password"
            required
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="password"
          />
        </div>
        <div>
          <label
            htmlFor="confirm-password"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Confirm Password
          </label>
          <input
            id="confirm-password"
            type="password"
            required
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            placeholder="password"
          />
        </div>
        <button
          type="submit"
          disabled={changePassword.isPending}
          className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {changePassword.isPending ? "Changing..." : "Change Password"}
        </button>
      </form>
    </div>
  );
}

// -- Field Input --

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: ConfigField;
  value: string | number | boolean;
  onChange: (val: string | number | boolean) => void;
}) {
  const [showSecret, setShowSecret] = useState(false);

  if (field.type === "dropdown" && field.options) {
    return (
      <select
        value={String(value ?? field.default ?? "")}
        onChange={(e) => onChange(e.target.value)}
        className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        <option value="">-- Select --</option>
        {field.options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    );
  }

  if (field.type === "boolean") {
    return (
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
          className="rounded border-gray-300"
        />
        <span className="text-sm text-gray-700">Enabled</span>
      </label>
    );
  }

  if (field.type === "number") {
    return (
      <input
        type="number"
        value={value === undefined || value === "" ? "" : Number(value)}
        onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
        className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
    );
  }

  if (field.type === "password") {
    return (
      <div className="relative">
        <input
          type={showSecret ? "text" : "password"}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 pr-10 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          placeholder="password"
        />
        <button
          type="button"
          onClick={() => setShowSecret(!showSecret)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
        >
          {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
      </div>
    );
  }

  return (
    <input
      type="text"
      value={String(value ?? "")}
      onChange={(e) => onChange(e.target.value)}
      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
    />
  );
}

// -- Config Editor --

function ConfigEditor() {
  const { data: schema, isLoading: schemaLoading } = useConfigSchema();
  const { data: settings, isLoading: settingsLoading } = useConfigSettings();
  const saveSettings = useSaveConfigSettings();
  const applyConfig = useApplyConfig();
  const clearConfigToken = useConfigStore((s) => s.clearConfigToken);

  const [values, setValues] = useState<Record<string, string | number | boolean>>({});
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [restartResult, setRestartResult] = useState<RestartResult | null>(null);
  // True while the page waits on a restart: its own Save and Restart, or one the
  // config service reports as running when the editor opens (issue #1433).
  const [awaitingRestart, setAwaitingRestart] = useState(false);
  // Aborted when the editor goes away, which ends any wait for a restart result.
  const pageLife = useRef<AbortController | null>(null);

  const showRestartResult = (result: RestartResult) => {
    setRestartResult(result);
    if (result.errors.length === 0) {
      toast.success(`Restarted ${result.restarted.length} services`);
    } else {
      toast.error(`Some services failed to restart`);
    }
  };

  // Issue #1433: the last restart the config service started, read when the editor
  // opens, so a reload during or after a restart shows its true outcome. A finished
  // one is shown as is; a running one is waited for, as a lost answer is.
  useEffect(() => {
    const life = new AbortController();
    pageLife.current = life;
    const follow = async () => {
      let last: LastRestart;
      try {
        last = await fetchLastRestart();
      } catch {
        return;
      }
      if (life.signal.aborted) return;
      if (last.state === "done" && last.result) {
        setRestartResult(last.result);
        return;
      }
      if (last.state !== "running") return;
      setAwaitingRestart(true);
      const outcome = await waitForRestart(fetchLastRestart, null, { signal: life.signal });
      if (outcome.kind === "cancelled") return;
      setAwaitingRestart(false);
      if (outcome.kind === "done") {
        showRestartResult(outcome.result);
      } else {
        toast.error(RESTART_FOLLOW_LOST_TEXT);
      }
    };
    void follow();
    return () => life.abort();
  }, []);

  // Populate form from settings when loaded
  const [settingsLoaded, setSettingsLoaded] = useState(false);
  if (settings && !settingsLoaded) {
    setValues(settings);
    setSettingsLoaded(true);
  }

  // Apply defaults from schema for fields not in settings
  useEffect(() => {
    if (!schema || !settingsLoaded) return;
    const updated = { ...values };
    let changed = false;
    for (const field of schema) {
      if (
        field.default !== undefined &&
        (updated[field.key] === undefined || updated[field.key] === "")
      ) {
        updated[field.key] = field.default;
        changed = true;
      }
    }
    // Intentional state sync: apply schema defaults once after settings load.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (changed) setValues(updated);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schema, settingsLoaded]);

  const groups = useMemo(() => {
    if (!schema) return [];
    const seen = new Set<string>();
    return schema
      .map((f) => f.group)
      .filter((g) => {
        if (seen.has(g)) return false;
        seen.add(g);
        return true;
      });
  }, [schema]);

  const toggleGroup = (group: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(group)) {
        next.delete(group);
      } else {
        next.add(group);
      }
      return next;
    });
  };

  const handleSave = async () => {
    try {
      await saveSettings.mutateAsync(values);
      toast.success("Configuration saved");
    } catch (err: unknown) {
      // The config service refuses an invalid value with 422 {errors: [string]}.
      const invalid = structuredDetail<{ errors: unknown[] }>(err, 422, (d) =>
        Array.isArray(d.errors),
      );
      const first = invalid?.errors[0];
      const msg =
        typeof first === "string" && first
          ? first
          : errorDetail(err, "Failed to save configuration");
      toast.error(msg);
    }
  };

  const reportLostAnswer = (outcome: Exclude<RestartOutcome, { kind: "done" }>) => {
    if (outcome.kind === "not_started") toast.error(RESTART_NOT_STARTED_TEXT);
    else if (outcome.kind === "timed_out") toast.error(RESTART_TIMED_OUT_TEXT);
    else if (outcome.kind === "unreadable") toast.error(RESTART_UNREADABLE_TEXT);
  };

  const handleSaveAndRestart = async () => {
    try {
      await saveSettings.mutateAsync(values);
    } catch {
      toast.error("Failed to save or restart");
      return;
    }
    toast.success("Configuration saved, restarting services...");
    setAwaitingRestart(true);
    try {
      // The record's id before this apply, so an older run is never taken for this one.
      let priorId: string | null;
      try {
        priorId = (await fetchLastRestart()).id;
      } catch {
        toast.error(RESTART_NOT_STARTED_TEXT);
        return;
      }
      let result: RestartResult;
      try {
        result = await applyConfig.mutateAsync();
      } catch (err) {
        // A refusal from the config service is a failure. A lost answer is not: the
        // restart itself can cut the request off (issue #1433), so the verdict comes
        // from the config service's record of the run.
        if (!answerWasLost(err)) {
          toast.error("Failed to save or restart");
          return;
        }
        const outcome = await waitForRestart(fetchLastRestart, priorId, {
          signal: pageLife.current?.signal,
        });
        if (outcome.kind !== "done") {
          reportLostAnswer(outcome);
          return;
        }
        result = outcome.result;
      }
      showRestartResult(result);
    } finally {
      setAwaitingRestart(false);
    }
  };

  if (schemaLoading || settingsLoading) {
    return (
      <div className="text-center py-12 text-gray-500">Loading configuration...</div>
    );
  }

  if (!schema) {
    return (
      <div className="text-center py-12 text-red-500">
        Failed to load configuration schema
      </div>
    );
  }

  return (
    <div className="w-full max-w-2xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">HERD Configuration</h1>
          <p className="text-sm text-gray-500 mt-1">
            Configure all HERD service settings
          </p>
        </div>
        <button
          onClick={() => clearConfigToken()}
          className="text-sm text-gray-500 hover:text-gray-700"
        >
          Sign out
        </button>
      </div>

      <div className="space-y-4">
        {groups.map((group) => {
          const groupFields = schema.filter((f) => f.group === group);
          const isCollapsed = collapsedGroups.has(group);
          return (
            <div
              key={group}
              className="bg-white rounded-xl shadow-sm border border-gray-200"
            >
              <button
                type="button"
                onClick={() => toggleGroup(group)}
                className="w-full flex items-center justify-between px-6 py-4 text-left"
              >
                <h2 className="text-sm font-semibold text-gray-900">{group}</h2>
                {isCollapsed ? (
                  <ChevronRight className="w-4 h-4 text-gray-400" />
                ) : (
                  <ChevronDown className="w-4 h-4 text-gray-400" />
                )}
              </button>
              {!isCollapsed && (
                <div className="px-6 pb-4 space-y-4">
                  {groupFields.map((field) => (
                    <div key={field.key}>
                      <label className="block text-sm font-medium text-gray-700 mb-1">
                        {field.label}
                        {field.required && (
                          <span className="text-red-500 ml-1">*</span>
                        )}
                      </label>
                      <FieldInput
                        field={field}
                        value={values[field.key] ?? ""}
                        onChange={(val) =>
                          setValues((prev) => ({ ...prev, [field.key]: val }))
                        }
                      />
                      <p className="text-xs text-gray-400 mt-1">{field.description}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {restartResult && (
        <div className="mt-4 bg-white rounded-xl shadow-sm border border-gray-200 p-4">
          <h3 className="text-sm font-semibold text-gray-900 mb-2">Restart Result</h3>
          {restartResult.restarted.length > 0 && (
            <p className="text-sm text-green-600">
              Restarted: {restartResult.restarted.join(", ")}
            </p>
          )}
          {restartResult.errors.length > 0 && (
            <div className="text-sm text-red-600 mt-1">
              {restartResult.errors.map((err, i) => (
                <p key={i}>{err}</p>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="flex gap-3 mt-6 mb-8">
        <button
          onClick={handleSave}
          disabled={saveSettings.isPending}
          className="flex-1 bg-gray-100 text-gray-700 py-2 rounded-lg text-sm font-medium hover:bg-gray-200 disabled:opacity-50 transition-colors"
        >
          {saveSettings.isPending ? "Saving..." : "Save"}
        </button>
        <button
          onClick={handleSaveAndRestart}
          disabled={saveSettings.isPending || applyConfig.isPending || awaitingRestart}
          className="flex-1 bg-blue-600 text-white py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {applyConfig.isPending || awaitingRestart ? "Restarting..." : "Save and Restart"}
        </button>
      </div>

      <p className="text-center text-sm text-gray-500 mb-8">
        <Link to="/login" className="text-blue-600 hover:underline font-medium">
          <ArrowLeft className="w-3 h-3 inline mr-1" />
          Back to login
        </Link>
      </p>
    </div>
  );
}

// -- Main Page --

export function ConfigPage() {
  const configToken = useConfigStore((s) => s.configToken);
  const [passwordChanged, setPasswordChanged] = useState<boolean | null>(null);

  const handleLoginSuccess = (changed: boolean) => {
    setPasswordChanged(changed);
  };

  // Not authenticated to config
  if (!configToken) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-gray-50">
        <ConfigLogin onLoginSuccess={handleLoginSuccess} />
        <footer className="mt-4 px-4 text-center">
          <AuthorCredit />
        </footer>
      </div>
    );
  }

  // Must change password first
  if (passwordChanged === false) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-gray-50">
        <PasswordChange onChanged={() => setPasswordChanged(true)} />
        <footer className="mt-4 px-4 text-center">
          <AuthorCredit />
        </footer>
      </div>
    );
  }

  // Config editor
  return (
    // Issue #988: the editor ends in Save, Save and Restart, and Back to login at
    // the bottom centre; the bottom padding lets them scroll clear of a toast.
    <div
      className={`min-h-screen flex items-center justify-center bg-gray-50 pt-8 ${TOAST_CLEARANCE_CLASS}`}
    >
      <ConfigEditor />
    </div>
  );
}
