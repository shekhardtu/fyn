"use client";

import { useEffect } from "react";

const ATTRIBUTE = "focusModality";

/**
 * Browsers may match :focus-visible for pointer-focused text fields. Keep the
 * app's field treatment stricter: only a Tab navigation enables focus rings.
 */
export function FocusModality() {
  useEffect(() => {
    const root = document.documentElement;
    const useKeyboard = (event: KeyboardEvent) => {
      if (event.key === "Tab") root.dataset[ATTRIBUTE] = "keyboard";
    };
    const usePointer = () => {
      root.dataset[ATTRIBUTE] = "pointer";
    };

    window.addEventListener("keydown", useKeyboard, true);
    window.addEventListener("pointerdown", usePointer, true);

    return () => {
      window.removeEventListener("keydown", useKeyboard, true);
      window.removeEventListener("pointerdown", usePointer, true);
      delete root.dataset[ATTRIBUTE];
    };
  }, []);

  return null;
}
