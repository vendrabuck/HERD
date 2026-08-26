import { isAdminRole } from "@/lib/roles";

describe("isAdminRole", () => {
  it("returns true for admin", () => {
    expect(isAdminRole("admin")).toBe(true);
  });

  it("returns true for superadmin", () => {
    expect(isAdminRole("superadmin")).toBe(true);
  });

  it("returns false for user", () => {
    expect(isAdminRole("user")).toBe(false);
  });

  it("returns false for undefined", () => {
    expect(isAdminRole(undefined)).toBe(false);
  });

  it("returns false for null", () => {
    expect(isAdminRole(null)).toBe(false);
  });

  it("returns false for an empty string", () => {
    expect(isAdminRole("")).toBe(false);
  });

  it("is case-sensitive: Admin does not count", () => {
    expect(isAdminRole("Admin")).toBe(false);
  });
});
