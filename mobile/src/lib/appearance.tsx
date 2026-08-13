import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useColorScheme } from "react-native";

import { cardShadowFor, chartPaletteFor, overlayShadowFor, paletteFor, type Palette, type Scheme } from "@/lib/theme";

/**
 * Which appearance the app is wearing.
 *
 * Follows the system, with no in-app override. That is a deliberate omission
 * rather than a missing feature: iOS already owns this setting, people already
 * know where it lives, and a second switch that disagrees with the first is a
 * bug report waiting to be filed. If a per-app override is ever wanted, this is
 * the one place it would be added.
 */

type Appearance = {
  scheme: Scheme;
  color: Palette;
  chartPalette: readonly string[];
  cardShadow: ReturnType<typeof cardShadowFor>;
  overlayShadow: ReturnType<typeof overlayShadowFor>;
};

const AppearanceContext = createContext<Appearance | null>(null);

export function AppearanceProvider({ children }: { children: ReactNode }) {
  // `useColorScheme` re-renders on the system switch, so everything below
  // re-derives without any subscription of our own.
  const scheme: Scheme = useColorScheme() === "dark" ? "dark" : "light";

  const value = useMemo<Appearance>(() => {
    const color = paletteFor(scheme);
    return {
      scheme,
      color,
      chartPalette: chartPaletteFor(color),
      cardShadow: cardShadowFor(color),
      overlayShadow: overlayShadowFor(color),
    };
  }, [scheme]);

  return <AppearanceContext.Provider value={value}>{children}</AppearanceContext.Provider>;
}

function appearance(): Appearance {
  const value = useContext(AppearanceContext);
  if (!value) throw new Error("useTheme was called outside AppearanceProvider");
  return value;
}

/** The palette, for colours read inline in a component. */
export function useTheme(): Palette {
  return appearance().color;
}

export function useAppearance(): Appearance {
  return appearance();
}

/**
 * A stylesheet that follows the appearance.
 *
 * `StyleSheet.create` runs once, at module load, so any colour baked into one
 * is frozen in whichever appearance happened to be active first. Passing the
 * palette into a factory instead defers that to render — and naming the
 * factory's parameter `color` means the body of an existing stylesheet needs no
 * edits at all to become theme-aware.
 *
 * The result is cached per palette, so a screen that re-renders on every
 * keystroke is not rebuilding its stylesheet each time.
 */
export function useStyles<T>(factory: (color: Palette) => T): T {
  const { color } = appearance();
  return useMemo(() => factory(color), [factory, color]);
}
