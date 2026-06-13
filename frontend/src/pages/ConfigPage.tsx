import { useState, useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import toast from "react-hot-toast";
import { ArrowLeft, Eye, EyeOff, ChevronDown, ChevronRight } from "lucide-react";
import { useConfigStore } from "@/stores/configStore";
import {
  useConfigLogin,
  useChangeConfigPassword,
  useConfigSchema,
  useConfigSettings,
  useSaveConfigSettings,
  useApplyConfig,
  type ConfigField,
} from "@/api/config";

// -- Config Login --

function ConfigLogin({
  onLoginSuccess,
}: {
  onLoginSuccess: (passwordChanged: boolean) => void;
}) {
  const [password, setPassword] = useState("");
  const login = useConfigLogin();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const result = await login.mutateAsync(password);
      onLoginSuccess(result.password_changed);
    } catch {
      toast.error("Invalid config password");
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
          disabled={login.isPending}
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
    if (newPassword.length < 8 || newPassword.length > 32) {
      toast.error("Password must be 8 to 32 characters");
      return;
    }
    try {
      await changePassword.mutateAsync(newPassword);
      toast.success("Config password changed");
      onChanged();
    } catch {
      toast.error("Failed to change password");
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
          <input
            id="new-password"
            type="password"
            required
            minLength={8}
            maxLength={32}
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
            minLength={8}
            maxLength={32}
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
  const [restartResult, setRestartResult] = useState<{
    restarted: string[];
    errors: string[];
  } | null>(null);

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
      const msg =
        (err as { response?: { data?: { detail?: { errors?: string[] } } } })?.response
          ?.data?.detail?.errors?.[0] ?? "Failed to save configuration";
      toast.error(msg);
    }
  };

  const handleSaveAndRestart = async () => {
    try {
      await saveSettings.mutateAsync(values);
      toast.success("Configuration saved, restarting services...");
      const result = await applyConfig.mutateAsync();
      setRestartResult(result);
      if (result.errors.length === 0) {
        toast.success(`Restarted ${result.restarted.length} services`);
      } else {
        toast.error(`Some services failed to restart`);
      }
    } catch {
      toast.error("Failed to save or restart");
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
          disabled={saveSettings.isPending || applyConfig.isPending}
          className="flex-1 bg-blue-600 text-white py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {applyConfig.isPending ? "Restarting..." : "Save and Restart"}
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
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <ConfigLogin onLoginSuccess={handleLoginSuccess} />
      </div>
    );
  }

  // Must change password first
  if (passwordChanged === false) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <PasswordChange onChanged={() => setPasswordChanged(true)} />
      </div>
    );
  }

  // Config editor
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 py-8">
      <ConfigEditor />
    </div>
  );
}
