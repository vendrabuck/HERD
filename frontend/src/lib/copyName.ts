// The name a Duplicate action gives its copy: "Copy of <name>", always within the
// name column the copy is written to. A device name and a template name are each at
// most 255 characters (INV-COL-1), and the server counts characters (code points),
// not UTF-16 units, so the cut is made on code points: a character outside the BMP
// is never split. The source name is cut from its end; the prefix is kept whole.
export const COPY_NAME_PREFIX = "Copy of ";
export const NAME_MAX_LENGTH = 255;

export function copyName(name: string, maxLength: number = NAME_MAX_LENGTH): string {
  const room = maxLength - Array.from(COPY_NAME_PREFIX).length;
  const chars = Array.from(name);
  return COPY_NAME_PREFIX + (chars.length > room ? chars.slice(0, room).join("") : name);
}
