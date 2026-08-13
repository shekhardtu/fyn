import { useMutation } from "@tanstack/react-query";
import * as Haptics from "expo-haptics";
import { router } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { Banner, Button, Field, FieldLabel, Type } from "@/components/ui";
import { ApiError, startSignInCode, verifySignInCode, type OtpChannel, type OtpSent } from "@/lib/api";
import { GoogleSignInButton, googleSignInConfigured } from "@/components/google-sign-in";
import { radius, space, type Palette } from "@/lib/theme";
import { useStyles, useTheme } from "@/lib/appearance";

const COPY: Record<OtpChannel, { label: string; noun: string; placeholder: string; hint: string }> = {
  phone: { label: "Phone", noun: "number", placeholder: "9876543210", hint: "We’ll text a six-digit code." },
  email: { label: "Email", noun: "address", placeholder: "you@example.com", hint: "We’ll email a six-digit code." },
};

/** Ten local digits, however they were typed or pasted. */
function localPhoneDigits(input: string) {
  return input.replace(/\D/g, "").replace(/^(?:0|91)/, "").slice(0, 10);
}

/** Counts down the resend window the server told us to wait out. */
function useCooldown() {
  const [remaining, setRemaining] = useState(0);
  const timer = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  useEffect(() => () => clearInterval(timer.current), []);

  const start = (seconds: number) => {
    clearInterval(timer.current);
    setRemaining(Math.max(0, Math.round(seconds)));
    timer.current = setInterval(() => {
      setRemaining((value) => {
        if (value <= 1) { clearInterval(timer.current); return 0; }
        return value - 1;
      });
    }, 1000);
  };

  return [remaining, start] as const;
}

export default function SignInScreen() {
  const styles = useStyles(makeStyles);
  const color = useTheme();
  const insets = useSafeAreaInsets();
  const [channel, setChannel] = useState<OtpChannel>("phone");
  const [value, setValue] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<OtpSent | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [remaining, startCooldown] = useCooldown();
  const codeRef = useRef<TextInput>(null);


  const copy = COPY[channel];
  const identifier = channel === "phone" ? `+91${value}` : value.trim();
  const identifierReady = channel === "phone" ? value.length === 10 : /.+@.+\..+/.test(value.trim());

  const request = useMutation({
    mutationFn: () => startSignInCode(channel, identifier),
    onMutate: () => setProblem(null),
    onSuccess: (result) => {
      setSent(result);
      setCode("");
      startCooldown(result.resendAfterSeconds);
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      // The field the person is now expected to fill should already be theirs.
      setTimeout(() => codeRef.current?.focus(), 250);
    },
    onError: (error: Error) => {
      setProblem(error.message);
      if (error instanceof ApiError && error.retryAfterSeconds) startCooldown(error.retryAfterSeconds);
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
    },
  });

  const verify = useMutation({
    mutationFn: () => verifySignInCode(sent!.challengeId, code.trim()),
    onMutate: () => setProblem(null),
    onSuccess: () => {
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
      // `replace`, not `push`: signing in is not somewhere to come back to.
      router.replace("/");
    },
    onError: (error: Error) => {
      setProblem(error.message);
      setCode("");
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Error);
    },
  });

  function changeChannel(next: OtpChannel) {
    if (next === channel) return;
    void Haptics.selectionAsync();
    setChannel(next);
    setValue("");
    setCode("");
    setSent(null);
    setProblem(null);
  }

  return (
    <KeyboardAvoidingView style={styles.root} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <ScrollView
        contentContainerStyle={[styles.scroll, { paddingTop: insets.top + space.section, paddingBottom: insets.bottom + space.section }]}
        keyboardShouldPersistTaps="handled"
        keyboardDismissMode="interactive"
      >
        <Type size="display" weight="semibold" color="ink" style={{ letterSpacing: -0.8 }}>fyn AI</Type>
        <Type size="body" color="muted" style={{ marginTop: space.snug }}>
          Sign in to your private workspace. No password to remember — we send a code.
        </Type>

        <View style={styles.switcher}>
          {(Object.keys(COPY) as OtpChannel[]).map((option) => {
            const active = option === channel;
            return (
              <Pressable
                key={option}
                onPress={() => changeChannel(option)}
                accessibilityRole="tab"
                accessibilityState={{ selected: active }}
                style={[styles.switch, active && styles.switchActive]}
              >
                <Type size="control" weight={active ? "semibold" : "medium"} color={active ? "ink" : "muted"}>{COPY[option].label}</Type>
              </Pressable>
            );
          })}
        </View>

        <View style={{ marginTop: space.loose }}>
          <FieldLabel hint={sent ? undefined : copy.hint}>{`Your ${copy.noun}`}</FieldLabel>
          <View style={styles.identifierRow}>
            {channel === "phone" ? (
              <View style={styles.prefix}><Type size="control" color="muted" tabular>+91</Type></View>
            ) : null}
            <Field
              value={value}
              onChangeText={(next) => setValue(channel === "phone" ? localPhoneDigits(next) : next)}
              placeholder={copy.placeholder}
              editable={!sent && !request.isPending}
              keyboardType={channel === "phone" ? "number-pad" : "email-address"}
              textContentType={channel === "phone" ? "telephoneNumber" : "emailAddress"}
              autoCapitalize="none"
              autoCorrect={false}
              maxLength={channel === "phone" ? 10 : undefined}
              accessibilityLabel={channel === "phone" ? "10-digit mobile number, country code +91" : "Email address"}
              style={[{ flex: 1 }, channel === "phone" && styles.phoneField, sent ? { backgroundColor: color.sunken, color: color.inkMuted } : null]}
              onSubmitEditing={() => { if (identifierReady && !sent) request.mutate(); }}
              returnKeyType="send"
            />
          </View>
        </View>

        {sent ? (
          <View style={{ marginTop: space.gutter }}>
            <FieldLabel hint={`Sent to ${sent.destinationMasked}.`}>Six-digit code</FieldLabel>
            <Field
              ref={codeRef}
              value={code}
              onChangeText={(next) => setCode(next.replace(/\D/g, "").slice(0, 6))}
              placeholder="••••••"
              keyboardType="number-pad"
              textContentType="oneTimeCode"
              autoComplete="one-time-code"
              maxLength={6}
              accessibilityLabel="Six-digit sign-in code"
              style={styles.codeField}
              onSubmitEditing={() => { if (code.length === 6) verify.mutate(); }}
              returnKeyType="go"
            />
            {/* Development only: the server echoes the code when OTP_DEBUG_ECHO
                is on, and typing it by hand off another screen is friction that
                exists for nobody. */}
            {sent.debugCode ? (
              <Pressable onPress={() => setCode(sent.debugCode!)} style={styles.debugCode}>
                <Type size="meta" color="muted">Development code — tap to fill: </Type>
                <Type size="meta" weight="semibold" color="secondary" tabular>{sent.debugCode}</Type>
              </Pressable>
            ) : null}
          </View>
        ) : null}

        {problem ? <View style={{ marginTop: space.gutter }}><Banner>{problem}</Banner></View> : null}

        <View style={{ marginTop: space.loose, gap: space.snug }}>
          {sent ? (
            <>
              <Button block size="field" onPress={() => verify.mutate()} disabled={code.length !== 6} busy={verify.isPending}>
                Sign in
              </Button>
              <Button
                block
                size="field"
                variant="ghost"
                onPress={() => request.mutate()}
                disabled={remaining > 0 || request.isPending}
              >
                {remaining > 0 ? `Resend in ${remaining}s` : "Send another code"}
              </Button>
            </>
          ) : (
            <Button block size="field" onPress={() => request.mutate()} disabled={!identifierReady} busy={request.isPending}>
              Send code
            </Button>
          )}
        </View>

        {googleSignInConfigured() ? (
          <View style={{ marginTop: space.loose, gap: space.base }}>
            <View style={styles.orRow}>
              <View style={styles.rule} />
              <Type size="meta" color="muted">OR</Type>
              <View style={styles.rule} />
            </View>
            <GoogleSignInButton
              onSignedIn={() => {
                void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success);
                router.replace("/");
              }}
              onProblem={setProblem}
            />
          </View>
        ) : null}

        <Type size="note" color="muted" style={{ marginTop: space.loose, textAlign: "center" }}>
          Your financial data stays in your own workspace. We never post on your behalf.
        </Type>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const makeStyles = (color: Palette) => StyleSheet.create({
  root: { flex: 1, backgroundColor: color.surface },
  scroll: { paddingHorizontal: space.loose, flexGrow: 1, justifyContent: "center" },
  switcher: {
    flexDirection: "row",
    gap: space.tight,
    marginTop: space.loose,
    padding: space.tight,
    borderRadius: radius.control,
    backgroundColor: color.sunken,
  },
  switch: { flex: 1, alignItems: "center", justifyContent: "center", height: 36, borderRadius: radius.chip },
  switchActive: { backgroundColor: color.surface },
  identifierRow: { flexDirection: "row", alignItems: "stretch" },
  prefix: {
    justifyContent: "center",
    paddingHorizontal: space.base,
    borderWidth: 1,
    borderRightWidth: 0,
    borderColor: color.lineStrong,
    borderTopLeftRadius: radius.control,
    borderBottomLeftRadius: radius.control,
    backgroundColor: color.sunken,
  },
  phoneField: { borderTopLeftRadius: 0, borderBottomLeftRadius: 0 },
  codeField: { fontSize: 22, letterSpacing: 8, textAlign: "center", fontVariant: ["tabular-nums"] },
  debugCode: { flexDirection: "row", alignItems: "center", justifyContent: "center", marginTop: space.snug, paddingVertical: space.snug },
  orRow: { flexDirection: "row", alignItems: "center", gap: space.base },
  rule: { flex: 1, height: StyleSheet.hairlineWidth, backgroundColor: color.line },
});
