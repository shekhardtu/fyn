/**
 * The same system the web app publishes in `globals.css`, expressed as values
 * React Native can consume — in both appearances.
 *
 * White (or near-black) is the ground. One secondary carries every interactive
 * surface, and it is deliberately not a money colour: green and red mean income
 * and expense and nothing else, so a filled control can never be misread as a
 * judgement about a number. No button is ever green.
 *
 * A literal colour in a component is a bug — there is a token for it, or the
 * token is missing.
 */

export type Palette = {
  /** Which appearance this is, so every derived value can take the palette
   *  alone rather than threading the scheme alongside it. */
  dark: boolean;

  surface: string;
  ground: string;
  sunken: string;

  ink: string;
  inkBody: string;
  inkMuted: string;

  line: string;
  lineSoft: string;
  lineStrong: string;

  secondary: string;
  secondaryHover: string;
  secondaryTint: string;
  secondaryLine: string;
  onSecondary: string;

  moneyIn: string;
  moneyOut: string;

  danger: string;
  dangerInk: string;
  dangerTint: string;
  dangerLine: string;
  attention: string;
  attentionTint: string;
};

const light: Palette = {
  dark: false,

  // Grounds
  surface: "#ffffff",
  ground: "#fafafb",
  sunken: "#f2f3f6",

  // Ink
  ink: "#101116",
  inkBody: "#3c4048",
  inkMuted: "#6c727c",

  // Rules
  line: "#e5e7ec",
  lineSoft: "#eef0f4",
  lineStrong: "#d0d4dc",

  // The one interactive colour
  secondary: "#4340e0",
  secondaryHover: "#3733c4",
  secondaryTint: "#eeeefd",
  secondaryLine: "#c9c8f7",
  onSecondary: "#ffffff",

  // Money. Data only — a control never wears these.
  moneyIn: "#0b7d57",
  moneyOut: "#c0432c",

  // Destructive and cautionary chrome, declared separately from the money hues
  // so tuning one cannot silently retint the other.
  danger: "#c0432c",
  dangerInk: "#a63722",
  dangerTint: "#fdf1ee",
  dangerLine: "#f3cfc6",
  attention: "#9c5f19",
  attentionTint: "#fdf6ec",
};

/**
 * Dark is not the light palette inverted.
 *
 * Two things change shape. Elevation reverses — a raised surface is *lighter*
 * than the page rather than whiter-than-white with a shadow under it, because
 * shadows are close to invisible on a dark ground and borders end up doing the
 * work instead. And every hue has to be re-picked rather than flipped: the
 * indigo and the two money colours are chosen for contrast against white, and
 * at those values on a dark ground they read as muddy and fail contrast. Each
 * one here is lifted until it clears 4.5:1 against the surface it sits on.
 */
const dark: Palette = {
  dark: true,

  ground: "#0d0e11",
  surface: "#16181d",
  sunken: "#212429",

  ink: "#f3f4f7",
  inkBody: "#c6cad2",
  inkMuted: "#8b919c",

  line: "#2a2d34",
  lineSoft: "#22252b",
  lineStrong: "#3a3e47",

  secondary: "#6663f0",
  secondaryHover: "#7b78f5",
  secondaryTint: "#1c1b33",
  secondaryLine: "#383571",
  onSecondary: "#ffffff",

  moneyIn: "#3ecf8e",
  moneyOut: "#f0705a",

  danger: "#f0705a",
  dangerInk: "#ff8f7b",
  dangerTint: "#2a1714",
  dangerLine: "#4d2620",
  attention: "#e0a458",
  attentionTint: "#2a2115",
};

export type Scheme = "light" | "dark";

export function paletteFor(scheme: Scheme): Palette {
  return scheme === "dark" ? dark : light;
}

/** The light palette, for the handful of places that run before a provider
 *  exists — the very first frame, and the static navigator options. */
export const color: Palette = light;

/** Six sizes. The floor is 11 — nothing smaller is readable on a phone.
 *
 *  `control` is 14 rather than 13: it sets every button label and the line you
 *  write on, and at 13 those read as small print next to 15px prose, which is
 *  the wrong signal for the two things you actually operate. */
export const text = {
  display: 28,
  title: 19,
  body: 15,
  control: 14,
  note: 12,
  meta: 11,
} as const;

/** Four steps, not sixteen. Cards 12, controls 8, chips 6. */
export const radius = {
  chip: 6,
  control: 8,
  card: 12,
  sheet: 16,
} as const;

export const space = {
  hair: 2,
  tight: 4,
  snug: 8,
  base: 12,
  gutter: 16,
  wide: 20,
  loose: 24,
  section: 32,
} as const;

/** The visual box. The touch target is always ≥44 and is added by the control
 *  itself, never by growing these — which is what lets the boxes be this
 *  small. Fields keep more height than buttons because a caret and a full line
 *  of typed text need the room; a label does not. */
export const height = {
  compact: 26,
  control: 32,
  lg: 36,
  field: 40,
  /** Apple's minimum, and the reason the boxes above may stay small. */
  touch: 44,
} as const;

/** Four durations, one easing. */
export const motion = {
  state: 110,
  enter: 180,
  surface: 240,
  overlay: 320,
} as const;

/** Series read left to right in the same order as the legend beside them.
 *
 *  The dark set is not the light set brightened: a chart is a field of large
 *  colour areas, and the light palette's depth reads as mud at that size. */
const chartLight = ["#4340e0", "#c98f4b", "#0b7d57", "#a6674f", "#3f7f9e", "#8e6c9c", "#8a9a5b", "#b0703f"] as const;
const chartDark = ["#8b88ff", "#e0a458", "#3ecf8e", "#e08f76", "#6bb6d6", "#bb9bd0", "#b4c67c", "#dba06a"] as const;

export function chartPaletteFor(color: Palette): readonly string[] {
  return color.dark ? chartDark : chartLight;
}

export const chartPalette = chartLight;

/** One shadow, and only surfaces that leave the document may use it. On a dark
 *  ground a shadow is nearly invisible, so the border carries the separation
 *  and the shadow is dialled back rather than faked louder. */
export function overlayShadowFor(color: Palette) {
  return {
    shadowColor: "#000000",
    shadowOpacity: color.dark ? 0.5 : 0.14,
    shadowRadius: 20,
    shadowOffset: { width: 0, height: 12 },
    elevation: 12,
  } as const;
}

/** The flat resting elevation cards use. Deliberately far weaker than the
 *  overlay shadow: a card is in the document, it has not left it. */
export function cardShadowFor(color: Palette) {
  return {
    shadowColor: "#000000",
    shadowOpacity: color.dark ? 0.3 : 0.05,
    shadowRadius: 10,
    shadowOffset: { width: 0, height: 3 },
    elevation: 2,
  } as const;
}

export const overlayShadow = overlayShadowFor(light);
export const cardShadow = cardShadowFor(light);
