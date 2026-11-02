import { describe, it, expect } from "vitest";
import { displayName, profileEditPayload, profileFormFields } from "@/lib/profileEdit";
import type { User } from "@/types/auth.types";

const USER: User = {
  id: "u1",
  email: "ada@login.example.com",
  username: "ada",
  full_name: "Ada Lovelace",
  contact_email: "ada@mail.example.com",
  phone: "+44 20 7946 0000",
  is_active: true,
  role: "user",
  created_at: "2026-01-01T00:00:00Z",
};

const BASE = profileFormFields(USER);

describe("profileFormFields (issue #1252)", () => {
  it("maps null optional fields to empty strings", () => {
    expect(profileFormFields({ ...USER, contact_email: null, phone: null })).toEqual({
      full_name: "Ada Lovelace",
      contact_email: "",
      phone: "",
    });
  });
});

describe("profileEditPayload (issue #1252)", () => {
  it("is empty when nothing changed", () => {
    expect(profileEditPayload(BASE, { ...BASE })).toEqual({});
  });

  it("ignores white space around an unchanged value", () => {
    expect(profileEditPayload(BASE, { ...BASE, full_name: "  Ada Lovelace " })).toEqual({});
  });

  it("sends only the changed name, trimmed", () => {
    expect(profileEditPayload(BASE, { ...BASE, full_name: "  Countess Ada " })).toEqual({
      full_name: "Countess Ada",
    });
  });

  it("sends a cleared contact email and phone as null", () => {
    expect(profileEditPayload(BASE, { ...BASE, contact_email: "  ", phone: "" })).toEqual({
      contact_email: null,
      phone: null,
    });
  });

  it("sends a newly set optional field trimmed", () => {
    const empty = { full_name: "Ada", contact_email: "", phone: "" };
    expect(profileEditPayload(empty, { ...empty, phone: " 555 0100 " })).toEqual({
      phone: "555 0100",
    });
  });

  it("sends a blank name as an empty string so the server refuses it", () => {
    expect(profileEditPayload(BASE, { ...BASE, full_name: "   " })).toEqual({ full_name: "" });
  });
});

describe("displayName (issue #1252)", () => {
  it("prefers the full name", () => {
    expect(displayName(USER)).toBe("Ada Lovelace");
  });

  it("falls back to the username when the name is empty or missing", () => {
    expect(displayName({ ...USER, full_name: "  " })).toBe("ada");
    expect(displayName({ username: "ada" } as User)).toBe("ada");
  });
});
