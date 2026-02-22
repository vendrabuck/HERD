import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { useRegister } from "@/api/auth";

export function RegisterPage() {
  const navigate = useNavigate();
  const register = useRegister();
  const [form, setForm] = useState({ email: "", username: "", password: "" });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await register.mutateAsync(form);
      toast.success("Account created. Please sign in.");
      navigate("/login");
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        "Registration failed";
      toast.error(msg);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm bg-white rounded-xl shadow-sm border border-gray-200 p-8">
        <div className="mb-6 text-center">
          <h1 className="text-2xl font-bold text-gray-900">HERD</h1>
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

          <button
            type="submit"
            disabled={register.isPending}
            aria-busy={register.isPending}
            className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {register.isPending ? "Creating account..." : "Create account"}
          </button>
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
