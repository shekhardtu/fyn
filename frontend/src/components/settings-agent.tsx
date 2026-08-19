import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, FileSpreadsheet, Lock, Table2 } from "lucide-react";
import { ChoiceRows, NotLiveStamp, PanelHeading, SettingSwitch, SettingsGroup, settingsProblem, settingsSaved } from "@/components/settings-parts";
import { getAgentSettings, setAnswerStyle, setAnswerValidationMode, type AnswerStyle, type AnswerValidationMode } from "@/lib/api";
import { cn } from "@/lib/utils";

const ANSWER_STYLE_OPTIONS: Array<{
  value: AnswerStyle;
  label: string;
  description: string;
  note?: string;
}> = [
  {
    value: "explained",
    label: "Explained",
    description: "Explain every financial result shown. Even a simple lookup gets one or two useful sentences interpreting its count, total, classification, or scope; complex questions get a deeper mental model.",
    note: "Recommended",
  },
  {
    value: "concise",
    label: "Concise",
    description: "Give the conclusion, the smallest useful evidence table or list, and one scope or comparison note.",
  },
];

const VALIDATION_OPTIONS: Array<{
  value: AnswerValidationMode;
  label: string;
  description: string;
  note?: string;
}> = [
  { value: "full", label: "Full", description: "Check evidence and requested comparisons; repair an incomplete answer once.", note: "Recommended" },
  { value: "evidence_only", label: "Evidence only", description: "Check financial claims against tool results, but skip completeness and repair." },
  { value: "off", label: "Off", description: "Publish read answers without grounding, evidence, or completeness checks. Useful for controlled testing." },
];

export function AnswerStyleChoices({
  value,
  disabled = false,
  onChange,
}: {
  value: AnswerStyle;
  disabled?: boolean;
  onChange: (style: AnswerStyle) => void;
}) {
  return <ChoiceRows label="Answer style" value={value} options={ANSWER_STYLE_OPTIONS} disabled={disabled} onChange={onChange} />;
}

export function AnswerValidationChoices({
  value,
  disabled = false,
  onChange,
}: {
  value: AnswerValidationMode;
  disabled?: boolean;
  onChange: (mode: AnswerValidationMode) => void;
}) {
  return <ChoiceRows label="Answer validation mode" value={value} options={VALIDATION_OPTIONS} disabled={disabled} onChange={onChange} />;
}

/** How much fyn AI does before it asks. Today's behaviour is the middle row:
 *  a complete entry is filed and reported, and anything ambiguous stops for a
 *  confirmation. The control is a placeholder until the backend can store the
 *  choice, so it is inert and says so rather than pretending to save. */
const AUTONOMY_OPTIONS = [
  { value: "ask", label: "Ask before every entry", description: "Nothing is filed until you confirm it. Slowest, and nothing reaches your ledger unseen." },
  { value: "file_and_tell", label: "File complete entries, tell you after", description: "A message with an amount, a date and a merchant is filed straight away; anything missing stops for a question." },
  { value: "file_quietly", label: "File without a receipt", description: "Entries are filed with no confirmation card. The transactions page stays the record." },
] as const;

/** The four families of thing the analyst can reach for. Toggling one is a
 *  placeholder; the list itself is not — these are the tool families the
 *  runtime actually binds. */
const TOOL_FAMILIES: Array<{ id: string; label: string; description: string; icon: typeof Table2; enabled: boolean }> = [
  { id: "stored", label: "Stored analyses", description: "Validated, reusable analyses with named parameters.", icon: Table2, enabled: true },
  { id: "sql", label: "Governed SQL", description: "One read-only query at a time over your published tables.", icon: Database, enabled: true },
  { id: "uploads", label: "Files you upload", description: "CSVs and statements attached to a conversation.", icon: FileSpreadsheet, enabled: true },
  { id: "external", label: "Connected databases", description: "Sources you link yourself. None are connected.", icon: Database, enabled: false },
];

/** The boundaries with no switch.
 *
 *  Every settings page shows what you can change; almost none show what you
 *  cannot. The research on agentic interfaces is consistent that people can
 *  only calibrate trust against a boundary they can see, and these four are
 *  enforced in the backend rather than described here:
 *
 *    · scoping    — `sql_gate.apply_emulated_rls` rewrites every query with
 *                   your account id, plus the `app.current_user_id` GUC
 *    · read-only  — `agents.safe_governed_read` refuses a resolved intent that
 *                   asks for anything but a governed read
 *    · manifest   — `sql_gate._reject_unknown_tables` (`unknown_table`)
 *    · one stmt   — `sql_gate.gate_sql` (`multiple_statements`,
 *                   `forbidden_construct`, `forbidden_function`)
 *
 *  If one of these ever gains a switch, it comes off this list the same day. */
const FIXED_GUARANTEES = [
  { label: "Your account only", detail: "Every query is rewritten with your account id before it runs. There is no query shape that reaches another person's ledger." },
  { label: "Reads, never writes", detail: "The analyst may run a governed read and nothing else. A plan that asks to change a record is refused before execution." },
  { label: "Published tables only", detail: "A query that reaches for a table outside the published set is refused, not filtered afterwards." },
  { label: "One statement, no side effects", detail: "Multiple statements, writes disguised as selects, and non-deterministic functions are all rejected at the gate." },
];

export function AgentSettingsPanel() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["agent-settings"], queryFn: getAgentSettings });

  const saveStyle = useMutation({
    mutationFn: setAnswerStyle,
    onSuccess: (result) => {
      queryClient.setQueryData(["agent-settings"], result);
      settingsSaved("Answer style saved.");
    },
    onError: (cause: Error) => settingsProblem(cause.message),
  });

  const saveValidation = useMutation({
    mutationFn: setAnswerValidationMode,
    onSuccess: (result) => {
      queryClient.setQueryData(["agent-settings"], result);
      settingsSaved("Answer checking saved.");
    },
    onError: (cause: Error) => settingsProblem(cause.message),
  });

  return <div>
    <PanelHeading
      title="Agent settings"
      blurb="What fyn AI may do while it answers you — and the four things it may never do, whatever you choose here."
    />

    {settings.isError ? <p role="alert" className="mt-4 rounded-lg border border-danger-line bg-danger-tint px-4 py-3 text-note leading-5 text-danger-ink">
      Agent settings couldn’t be loaded. <button type="button" onClick={() => settings.refetch()} className="font-semibold underline">Try again</button>
    </p> : null}
    {settings.isLoading ? <div role="status" aria-label="Loading agent settings" className="mt-4 h-40 animate-pulse rounded-lg bg-line/70" /> : null}

    <SettingsGroup
      title="Autonomy"
      stamp={<NotLiveStamp />}
      description="How much fyn AI does before it asks you. Today it files complete entries and tells you after; the choice below isn’t stored yet, so changing it wouldn’t change anything."
    >
      <ChoiceRows label="Autonomy level" value="file_and_tell" disabled options={AUTONOMY_OPTIONS.map((option) => ({ ...option, note: option.value === "file_and_tell" ? "How it works today" : undefined }))} />
    </SettingsGroup>

    <SettingsGroup
      title="Answer style"
      description="Choose whether fyn AI gives you the compact result or teaches the financial story behind it. This changes presentation, not the facts or safety boundaries."
    >
      {settings.data ? <AnswerStyleChoices
        value={settings.data.answerStyle}
        disabled={saveStyle.isPending}
        onChange={(style) => saveStyle.mutate(style)}
      /> : null}
    </SettingsGroup>

    <SettingsGroup
      title="Answer checking"
      description="What is verified before a read answer reaches you, whether or not the agent used a tool."
    >
      {settings.data ? <AnswerValidationChoices
        value={settings.data.answerValidationMode}
        disabled={saveValidation.isPending}
        onChange={(mode) => saveValidation.mutate(mode)}
      /> : null}
    </SettingsGroup>

    <SettingsGroup
      title="What it may use"
      stamp={<NotLiveStamp />}
      description="The tool families the analyst can reach for. The list is real; the switches aren’t stored yet."
    >
      <div className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface">
        {TOOL_FAMILIES.map((tool) => <SettingSwitch
          key={tool.id}
          variant="row"
          label={tool.label}
          description={tool.description}
          checked={tool.enabled}
          disabled
          icon={<tool.icon size={17} />}
          onChange={() => undefined}
        />)}
      </div>
    </SettingsGroup>

    <SettingsGroup
      title="Fixed, whatever you choose"
      description="These four have no switch, here or anywhere. They are enforced before a query runs, so they hold even with every check above turned off."
      className="settings-fixed"
    >
      <ol className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-surface-sunken">
        {FIXED_GUARANTEES.map((guarantee) => <li key={guarantee.label} className="flex items-start gap-3 px-4 py-3.5">
          <span aria-hidden className={cn("mt-0.5 grid size-6 shrink-0 place-items-center rounded-md border border-line bg-surface text-ink-muted")}><Lock size={12} /></span>
          <div className="min-w-0">
            <p className="text-control font-semibold text-ink-body">{guarantee.label}</p>
            <p className="mt-0.5 text-note leading-5 text-ink-muted">{guarantee.detail}</p>
          </div>
        </li>)}
      </ol>
    </SettingsGroup>
  </div>;
}
