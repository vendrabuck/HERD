import { useState } from "react";
import type { ProfileUpdate, User } from "@/types/auth.types";
import {
  FULL_NAME_MAX_LENGTH,
  PHONE_MAX_LENGTH,
  profileEditPayload,
  profileFormFields,
  type ProfileFormFields,
} from "@/lib/profileEdit";

const INPUT_CLASS =
  "w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500";

interface ProfileFormProps {
  user: User;
  // Prefixes the input ids so two forms never share one (Settings and the
  // Users page dialog can both be in the DOM in tests).
  idPrefix: string;
  isPending: boolean;
  onSave: (profile: ProfileUpdate) => void | Promise<void>;
  onCancel?: () => void;
}

/**
 * The profile editor shared by the Settings page and the Users page Edit
 * profile dialog (issue #1252). Name is required; contact email and phone are
 * optional. The sign-in email and username are shown read-only: they are the
 * login identity and no profile route changes them. Only changed fields are
 * sent (profileEditPayload). The parent remounts the form (a `key`) when the
 * saved account changes, which resets the baseline.
 */
export function ProfileForm({ user, idPrefix, isPending, onSave, onCancel }: ProfileFormProps) {
  const [baseline] = useState<ProfileFormFields>(() => profileFormFields(user));
  const [form, setForm] = useState<ProfileFormFields>(baseline);
  const payload = profileEditPayload(baseline, form);
  const nameBlank = form.full_name.trim() === "";
  const unchanged = Object.keys(payload).length === 0;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (nameBlank || unchanged) return;
    void onSave(payload);
  };

  const field = (key: keyof ProfileFormFields) => ({
    id: `${idPrefix}-${key.replace("_", "-")}`,
    value: form[key],
    onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((prev) => ({ ...prev, [key]: e.target.value })),
    className: INPUT_CLASS,
  });

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-gray-500">Sign-in email</dt>
        <dd className="text-gray-900 break-all">{user.email}</dd>
        <dt className="text-gray-500">Username</dt>
        <dd className="text-gray-900 font-mono">{user.username}</dd>
      </dl>

      <div>
        <label
          htmlFor={`${idPrefix}-full-name`}
          className="block text-sm font-medium text-gray-700 mb-1"
        >
          Name
        </label>
        <input type="text" required maxLength={FULL_NAME_MAX_LENGTH} {...field("full_name")} />
        {nameBlank && <p className="text-xs text-red-700 mt-1">Name is required.</p>}
      </div>

      <div>
        <label
          htmlFor={`${idPrefix}-contact-email`}
          className="block text-sm font-medium text-gray-700 mb-1"
        >
          Contact email
        </label>
        <input type="email" placeholder="Optional" {...field("contact_email")} />
        <p className="text-xs text-gray-500 mt-1">
          Where email notifications go. Leave blank to use the sign-in email.
        </p>
      </div>

      <div>
        <label htmlFor={`${idPrefix}-phone`} className="block text-sm font-medium text-gray-700 mb-1">
          Phone
        </label>
        <input type="tel" placeholder="Optional" maxLength={PHONE_MAX_LENGTH} {...field("phone")} />
        <p className="text-xs text-gray-500 mt-1">
          Stored and shown only. HERD does not call or text this number.
        </p>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="submit"
          disabled={isPending || nameBlank || unchanged}
          className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-700 transition-colors disabled:opacity-50"
        >
          {isPending ? "Saving..." : "Save profile"}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 transition-colors"
          >
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
