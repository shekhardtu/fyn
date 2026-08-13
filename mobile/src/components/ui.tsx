import * as Haptics from "expo-haptics";
import { forwardRef, type ReactNode } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
  type PressableProps,
  type StyleProp,
  type TextInputProps,
  type TextStyle,
  type ViewStyle,
} from "react-native";

import { cardShadowFor, height, radius, space, text, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";

/**
 * The primitives every screen is built from.
 *
 * The rule the web app states in `globals.css` holds here: a literal colour in
 * a component is a bug — there is a token for it, or the token is missing.
 */

// ── Type ─────────────────────────────────────────────────────────────────────

type ToneName = "ink" | "body" | "muted" | "secondary" | "in" | "out" | "danger" | "attention" | "onSecondary";

function tones(color: Palette): Record<ToneName, string> {
  return {
    ink: color.ink,
    body: color.inkBody,
    muted: color.inkMuted,
    secondary: color.secondary,
    in: color.moneyIn,
    out: color.moneyOut,
    danger: color.dangerInk,
    attention: color.attention,
    onSecondary: color.onSecondary,
  };
}

type TypeProps = {
  children: ReactNode;
  size?: keyof typeof text;
  weight?: "regular" | "medium" | "semibold";
  color?: ToneName;
  style?: StyleProp<TextStyle>;
  numberOfLines?: number;
  /** Lining, tabular figures, so columns of money align down the page. */
  tabular?: boolean;
  /** Overrides the ceiling below. */
  maxScale?: number;
} & Pick<TextInputProps, "accessibilityLabel">;

/**
 * How far each size may grow under Dynamic Type.
 *
 * Prose is what the accessibility setting is *for*, so body text is allowed to
 * run most of the way up. Chrome — button labels, eyebrows, axis figures — is
 * held tighter, because past about a third larger it stops fitting the control
 * it names and starts truncating, which helps nobody. Nothing is pinned at 1:
 * refusing to scale at all is the actual accessibility failure.
 */
const MAX_SCALE: Record<keyof typeof text, number> = {
  display: 1.5,
  title: 1.6,
  body: 1.8,
  control: 1.4,
  note: 1.5,
  meta: 1.3,
};

const weights = { regular: "400", medium: "500", semibold: "600" } as const;

export function Type({ children, size = "body", weight = "regular", color: named = "body", style, numberOfLines, tabular, maxScale, ...rest }: TypeProps) {
  const color = useTheme();
  return (
    <Text
      numberOfLines={numberOfLines}
      maxFontSizeMultiplier={maxScale ?? MAX_SCALE[size]}
      style={[
        {
          fontSize: text[size],
          fontWeight: weights[weight],
          color: tones(color)[named],
          // 1.45 keeps prose readable without the transcript feeling airy.
          lineHeight: Math.round(text[size] * 1.45),
        },
        tabular && { fontVariant: ["tabular-nums"] },
        style,
      ]}
      {...rest}
    >
      {children}
    </Text>
  );
}

/** The signature detail: every rupee figure in the product speaks with one
 *  voice — lining, tabular figures so columns of money align down the page. */
export function Money({ value, size = "body", weight = "semibold", color: named = "ink", style }: { value: string; size?: keyof typeof text; weight?: TypeProps["weight"]; color?: ToneName; style?: StyleProp<TextStyle> }) {
  return <Type size={size} weight={weight} color={named} tabular style={style}>{value}</Type>;
}

// ── Surfaces ─────────────────────────────────────────────────────────────────

export function Card({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  const styles = useStyles(makeStyles);
  return <View style={[styles.card, style]}>{children}</View>;
}

export function CardHeader({ eyebrow, title, body, caution, trailing }: { eyebrow?: string | null; title: string; body?: string | null; caution?: boolean; trailing?: ReactNode }) {
  const styles = useStyles(makeStyles);
  return (
    <View style={styles.cardHeader}>
      <View style={{ flex: 1, minWidth: 0 }}>
        {eyebrow ? <Type size="meta" weight="semibold" color={caution ? "attention" : "secondary"} style={styles.eyebrow}>{eyebrow.toUpperCase()}</Type> : null}
        <Type size="body" weight="semibold" color="ink" style={eyebrow ? { marginTop: space.tight } : undefined}>{title}</Type>
        {body ? <Type size="note" color="muted" style={{ marginTop: space.tight }}>{body}</Type> : null}
      </View>
      {trailing}
    </View>
  );
}

export function EmptyNote({ children }: { children: ReactNode }) {
  const styles = useStyles(makeStyles);
  return <Type size="note" color="muted" style={styles.emptyNote}>{children}</Type>;
}

export function Divider({ style }: { style?: StyleProp<ViewStyle> }) {
  const color = useTheme();
  return <View style={[{ height: StyleSheet.hairlineWidth, backgroundColor: color.line }, style]} />;
}

// ── Controls ─────────────────────────────────────────────────────────────────

type ButtonProps = {
  children: ReactNode;
  onPress?: () => void;
  variant?: "filled" | "outline" | "ghost" | "danger";
  size?: "control" | "lg" | "field";
  disabled?: boolean;
  busy?: boolean;
  /** Fills the row it sits in. */
  block?: boolean;
  style?: StyleProp<ViewStyle>;
} & Pick<PressableProps, "accessibilityLabel" | "accessibilityHint" | "testID">;

/**
 * The visual box comes from the size tokens; the touch target does not.
 *
 * `hitSlop` grows the tappable area to Apple's 44pt minimum without growing the
 * drawn control, which is the whole reason the boxes are allowed to be as small
 * as they are. A 32pt button that is only 32pt to a thumb is a missed tap.
 */
export function Button({ children, onPress, variant = "filled", size = "lg", disabled, busy, block, style, ...rest }: ButtonProps) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const inert = disabled || busy;
  const box = height[size];
  const slop = Math.max(0, Math.round((height.touch - box) / 2));

  return (
    <Pressable
      onPress={() => {
        if (inert) return;
        void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
        onPress?.();
      }}
      disabled={inert}
      hitSlop={{ top: slop, bottom: slop, left: space.snug, right: space.snug }}
      accessibilityRole="button"
      accessibilityState={{ disabled: !!inert, busy: !!busy }}
      style={({ pressed }) => [
        styles.button,
        { minHeight: box, opacity: inert ? 0.45 : 1 },
        block && { alignSelf: "stretch" },
        variant === "filled" && { backgroundColor: pressed ? color.secondaryHover : color.secondary },
        variant === "outline" && { backgroundColor: pressed ? color.sunken : color.surface, borderWidth: 1, borderColor: color.lineStrong },
        variant === "ghost" && { backgroundColor: pressed ? color.sunken : "transparent" },
        variant === "danger" && { backgroundColor: pressed ? color.dangerTint : color.surface, borderWidth: 1, borderColor: color.dangerLine },
        style,
      ]}
      {...rest}
    >
      {busy ? <ActivityIndicator size="small" color={variant === "filled" ? color.onSecondary : color.secondary} /> : null}
      {typeof children === "string" ? (
        <Type
          size="control"
          weight="medium"
          color={variant === "filled" ? "onSecondary" : variant === "danger" ? "danger" : "ink"}
        >
          {children}
        </Type>
      ) : (
        children
      )}
    </Pressable>
  );
}

/** A selectable option — the shape most HITL widgets are made of. */
export function Chip({ label, detail, selected, disabled, onPress }: { label: string; detail?: string | null; selected?: boolean; disabled?: boolean; onPress?: () => void }) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  return (
    <Pressable
      onPress={() => {
        if (disabled) return;
        void Haptics.selectionAsync();
        onPress?.();
      }}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityState={{ selected: !!selected, disabled: !!disabled }}
      hitSlop={{ top: space.tight, bottom: space.tight }}
      style={({ pressed }) => [
        styles.chip,
        selected && { borderColor: color.secondary, backgroundColor: color.secondaryTint },
        pressed && !disabled && { backgroundColor: color.sunken },
        disabled && { opacity: 0.45 },
      ]}
    >
      <Type size="control" weight={selected ? "semibold" : "medium"} color={selected ? "secondary" : "ink"}>{label}</Type>
      {detail ? <Type size="meta" color="muted" style={{ marginTop: 1 }}>{detail}</Type> : null}
    </Pressable>
  );
}

export function FieldLabel({ children, hint }: { children: ReactNode; hint?: string }) {
  const styles = useStyles(makeStyles);
  return (
    <View style={{ marginBottom: space.tight }}>
      <Type size="meta" weight="semibold" color="muted" style={styles.eyebrow}>{String(children).toUpperCase()}</Type>
      {hint ? <Type size="meta" color="muted" style={{ marginTop: 1 }}>{hint}</Type> : null}
    </View>
  );
}

export const Field = forwardRef<TextInput, TextInputProps & { invalid?: boolean }>(function Field({ invalid, style, ...rest }, ref) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  return (
    <TextInput
      ref={ref}
      maxFontSizeMultiplier={1.6}
      placeholderTextColor={color.inkMuted}
      style={[styles.field, invalid && { borderColor: color.dangerLine, backgroundColor: color.dangerTint }, style]}
      {...rest}
    />
  );
});

// ── Feedback ─────────────────────────────────────────────────────────────────

export function Banner({ children, tone: named = "danger", action }: { children: ReactNode; tone?: "danger" | "attention"; action?: ReactNode }) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const danger = named === "danger";
  return (
    <View
      accessibilityRole="alert"
      style={[styles.banner, { backgroundColor: danger ? color.dangerTint : color.attentionTint, borderColor: danger ? color.dangerLine : color.attention }]}
    >
      <Type size="note" color={danger ? "danger" : "attention"} style={{ flex: 1 }}>{children}</Type>
      {action}
    </View>
  );
}

export function Pill({ children, tone: named = "muted" }: { children: ReactNode; tone?: "muted" | "in" | "out" | "secondary" }) {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const backgrounds = { muted: color.sunken, in: "#e7f4ef", out: color.dangerTint, secondary: color.secondaryTint } as const;
  return (
    <View style={[styles.pill, { backgroundColor: backgrounds[named] }]}>
      <Type size="meta" weight="semibold" color={named === "muted" ? "muted" : named}>{children}</Type>
    </View>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  card: {
    borderRadius: radius.card,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: color.line,
    backgroundColor: color.surface,
    overflow: "hidden",
    ...cardShadowFor(color),
  },
  cardHeader: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: space.base,
    paddingHorizontal: space.gutter,
    paddingVertical: space.base,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: color.lineSoft,
  },
  eyebrow: { letterSpacing: 0.8 },
  emptyNote: { paddingHorizontal: space.gutter, paddingVertical: space.loose, textAlign: "center" },
  button: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: space.snug,
    paddingHorizontal: space.gutter,
    paddingVertical: space.tight,
    borderRadius: radius.control,
  },
  chip: {
    minHeight: height.touch,
    justifyContent: "center",
    paddingHorizontal: space.base,
    paddingVertical: space.snug,
    borderRadius: radius.control,
    borderWidth: 1,
    borderColor: color.line,
    backgroundColor: color.surface,
  },
  field: {
    minHeight: height.touch,
    paddingHorizontal: space.base,
    paddingVertical: space.snug,
    borderRadius: radius.control,
    borderWidth: 1,
    borderColor: color.lineStrong,
    backgroundColor: color.surface,
    fontSize: text.control,
    color: color.ink,
  },
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.base,
    padding: space.base,
    borderRadius: radius.control,
    borderWidth: 1,
  },
  pill: {
    alignSelf: "flex-start",
    paddingHorizontal: space.snug,
    paddingVertical: 3,
    borderRadius: radius.chip,
  },
});
