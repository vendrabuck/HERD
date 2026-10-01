import { purposeClassifyRefusal } from "@/lib/errors";

function axiosLike(status: number, detail: unknown) {
  return { response: { status, data: { detail } } };
}

describe("purposeClassifyRefusal", () => {
  it("maps 503 purpose_classification_disabled", () => {
    expect(
      purposeClassifyRefusal(axiosLike(503, { error: "purpose_classification_disabled" })),
    ).toBe("purpose_classification_disabled");
  });

  it("maps both 409 bodies", () => {
    expect(purposeClassifyRefusal(axiosLike(409, { error: "already_suggested" }))).toBe(
      "already_suggested",
    );
    expect(purposeClassifyRefusal(axiosLike(409, { error: "not_eligible" }))).toBe("not_eligible");
  });

  it("returns null for a mismatched status, an unknown error, a string detail, or a non-axios error", () => {
    expect(
      purposeClassifyRefusal(axiosLike(409, { error: "purpose_classification_disabled" })),
    ).toBeNull();
    expect(purposeClassifyRefusal(axiosLike(503, { error: "already_suggested" }))).toBeNull();
    expect(purposeClassifyRefusal(axiosLike(409, { error: "something_else" }))).toBeNull();
    expect(purposeClassifyRefusal(axiosLike(404, "Reservation not found"))).toBeNull();
    expect(purposeClassifyRefusal(axiosLike(503, "Service unavailable"))).toBeNull();
    expect(purposeClassifyRefusal(new Error("network"))).toBeNull();
  });
});
