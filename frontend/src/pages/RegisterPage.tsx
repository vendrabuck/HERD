import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { useRegister } from "@/api/auth";
import { Button } from "@/components/ui/Button";
import { profileSaveErrorText } from "@/lib/errors";
import { FULL_NAME_MAX_LENGTH } from "@/lib/profileEdit";

export function RegisterPage() {
  const navigate = useNavigate();
  const register = useRegister();
  const [form, setForm] = useState({ email: "", username: "", password: "" });
  // Issue #1252: the name is prefilled from the username and follows it until
  // the user edits the name field. Blank is sent as no name, which the API
  // stores as the username.
  const [fullName, setFullName] = useState("");
  const [nameEdited, setNameEdited] = useState(false);
  const shownName = nameEdited ? fullName : form.username;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const name = shownName.trim();
    try {
      await register.mutateAsync(name ? { ...form, full_name: name } : form);
      toast.success("Account created. Please sign in.");
      navigate("/login", { replace: true });
    } catch (err: unknown) {
      // A string detail (the 409 collision) as is; a 422 naming the name
      // field per field; anything else (a 422 list for another field) the
      // generic text, never a raw list, which would crash the Toaster.
      toast.error(profileSaveErrorText(err, "Registration failed"));
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm bg-white rounded-xl shadow-sm border border-gray-200 p-8">
        <div className="mb-6 text-center">
          <div className="flex items-center justify-center gap-3">
            <div className="w-12 h-12 rounded-lg bg-slate-800 flex items-center justify-center shrink-0">
              <span className="font-mono font-bold text-2xl text-sky-400">H</span>
            </div>
            <h1 className="text-2xl font-bold text-gray-900">HERD</h1>
          </div>
          <p className="text-sm text-gray-500 mt-1">Create your account</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="reg-email" className="block text-sm font-medium text-gray-700 mb-1">Email</label>
            <input
              id="reg-email"
              type="email"
              required
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="you@example.com"
            />
          </div>

          <div>
            <label htmlFor="reg-username" className="block text-sm font-medium text-gray-700 mb-1">Username</label>
            <input
              id="reg-username"
              type="text"
              required
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="johndoe"
            />
          </div>

          <div>
            <label htmlFor="reg-full-name" className="block text-sm font-medium text-gray-700 mb-1">Name</label>
            <input
              id="reg-full-name"
              type="text"
              maxLength={FULL_NAME_MAX_LENGTH}
              value={shownName}
              onChange={(e) => {
                setNameEdited(true);
                setFullName(e.target.value);
              }}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="John Doe"
            />
            <p className="text-xs text-gray-500 mt-1">Shown to other users. Defaults to your username.</p>
          </div>

          <div>
            <label htmlFor="reg-password" className="block text-sm font-medium text-gray-700 mb-1">Password</label>
            <input
              id="reg-password"
              type="password"
              required
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="password"
            />
          </div>

          <Button
            type="submit"
            variant="primary"
            disabled={register.isPending}
            aria-busy={register.isPending}
            className="w-full"
          >
            {register.isPending ? "Creating account..." : "Create account"}
          </Button>
        </form>

        <p className="text-center text-sm text-gray-500 mt-4">
          Already have an account?{" "}
          <Link to="/login" className="text-blue-600 hover:underline font-medium">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
