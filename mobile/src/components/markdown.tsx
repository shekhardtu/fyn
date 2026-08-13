import { Lexer, type Token, type Tokens } from "marked";
import { Fragment, memo, useMemo } from "react";
import { Linking, ScrollView, StyleSheet, Text, View } from "react-native";

import { Type } from "@/components/ui";
import { radius, space, text as textSize, type Palette } from "@/lib/theme";
import { useStyles } from "@/lib/appearance";

/**
 * Display-only rich text for an assistant's narrative.
 *
 * Raw HTML is deliberately unsupported. Financial records and actions remain
 * typed widgets; Markdown is only the explanatory layer around those widgets —
 * which is exactly why this parses to tokens and renders them itself rather
 * than reaching for an HTML renderer inside a WebView. A `<Text>` tree is what
 * lets a paragraph share a line box with the transcript around it, and it is
 * the only version of this that costs nothing to scroll.
 */

function safeHref(href: string | undefined) {
  if (!href) return null;
  // Only the two schemes a narrative has any business linking to. Everything
  // else — `javascript:`, `file:`, a custom scheme pointing at another app —
  // renders as plain text.
  return /^https?:\/\//i.test(href) ? href : null;
}

/** Inline tokens share the parent's line box, so they are `<Text>`, never
 *  `<View>`: a nested `<View>` would break the line and lose the wrap. */
function Inline({ tokens }: { tokens: Token[] | undefined }) {
  const styles = useStyles(makeStyles);
  if (!tokens?.length) return null;
  return (
    <>
      {tokens.map((token, index) => {
        switch (token.type) {
          case "strong":
            return <Text key={index} style={styles.strong}><Inline tokens={(token as Tokens.Strong).tokens} /></Text>;
          case "em":
            return <Text key={index} style={styles.em}><Inline tokens={(token as Tokens.Em).tokens} /></Text>;
          case "del":
            return <Text key={index} style={styles.del}><Inline tokens={(token as Tokens.Del).tokens} /></Text>;
          case "codespan":
            return <Text key={index} style={styles.codespan}>{(token as Tokens.Codespan).text}</Text>;
          case "link": {
            const link = token as Tokens.Link;
            const href = safeHref(link.href);
            if (!href) return <Text key={index}><Inline tokens={link.tokens} /></Text>;
            return (
              <Text key={index} style={styles.link} onPress={() => void Linking.openURL(href).catch(() => undefined)}>
                <Inline tokens={link.tokens} />
              </Text>
            );
          }
          case "br":
            return <Text key={index}>{"\n"}</Text>;
          case "escape":
            return <Text key={index}>{(token as Tokens.Escape).text}</Text>;
          default:
            return <Text key={index}>{(token as { raw?: string; text?: string }).text ?? (token as { raw?: string }).raw ?? ""}</Text>;
        }
      })}
    </>
  );
}

function ListBlock({ token, depth }: { token: Tokens.List; depth: number }) {
  const styles = useStyles(makeStyles);
  return (
    <View style={{ marginVertical: space.snug, gap: space.tight }}>
      {token.items.map((item, index) => (
        <View key={index} style={styles.listRow}>
          <Text style={styles.marker}>
            {token.ordered ? `${(Number(token.start) || 1) + index}.` : "•"}
          </Text>
          <View style={{ flex: 1, minWidth: 0 }}>
            <Blocks tokens={item.tokens} depth={depth + 1} />
          </View>
        </View>
      ))}
    </View>
  );
}

/** Tables scroll inside their own container. The page never scrolls sideways —
 *  a transcript that drifts horizontally because one reply had six columns is
 *  the single worst thing a chat layout can do on a phone. */
function TableBlock({ token }: { token: Tokens.Table }) {
  const styles = useStyles(makeStyles);
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.table} contentContainerStyle={{ minWidth: "100%" }}>
      <View>
        <View style={[styles.tableRow, styles.tableHead]}>
          {token.header.map((cell, index) => (
            <View key={index} style={[styles.tableCell, index === token.header.length - 1 && styles.tableCellLast]}>
              <Type size="meta" weight="semibold" color="muted" style={{ letterSpacing: 0.6 }}>{cell.text.toUpperCase()}</Type>
            </View>
          ))}
        </View>
        {token.rows.map((row, rowIndex) => (
          <View key={rowIndex} style={styles.tableRow}>
            {row.map((cell, index) => (
              <View key={index} style={[styles.tableCell, index === row.length - 1 && styles.tableCellLast]}>
                <Type size="note" color="body"><Inline tokens={cell.tokens} /></Type>
              </View>
            ))}
          </View>
        ))}
      </View>
    </ScrollView>
  );
}

function Blocks({ tokens, depth = 0 }: { tokens: Token[]; depth?: number }) {
  const styles = useStyles(makeStyles);
  return (
    <>
      {tokens.map((token, index) => {
        switch (token.type) {
          case "paragraph":
            return (
              <Type key={index} size="body" color="body" style={depth ? undefined : styles.paragraph}>
                <Inline tokens={(token as Tokens.Paragraph).tokens} />
              </Type>
            );
          case "text": {
            const inline = token as Tokens.Text;
            return (
              <Type key={index} size="body" color="body">
                {inline.tokens?.length ? <Inline tokens={inline.tokens} /> : inline.text}
              </Type>
            );
          }
          case "heading": {
            const heading = token as Tokens.Heading;
            return (
              <Type
                key={index}
                size={heading.depth <= 2 ? "title" : "body"}
                weight="semibold"
                color="ink"
                style={styles.heading}
              >
                <Inline tokens={heading.tokens} />
              </Type>
            );
          }
          case "list":
            return <ListBlock key={index} token={token as Tokens.List} depth={depth} />;
          case "blockquote":
            return (
              <View key={index} style={styles.quote}>
                <Blocks tokens={(token as Tokens.Blockquote).tokens} depth={depth + 1} />
              </View>
            );
          case "code":
            return (
              <ScrollView key={index} horizontal showsHorizontalScrollIndicator={false} style={styles.pre}>
                <Text style={styles.preText}>{(token as Tokens.Code).text}</Text>
              </ScrollView>
            );
          case "table":
            return <TableBlock key={index} token={token as Tokens.Table} />;
          case "hr":
            return <View key={index} style={styles.rule} />;
          case "space":
            return null;
          default:
            return <Fragment key={index} />;
        }
      })}
    </>
  );
}

export const Markdown = memo(function Markdown({ children }: { children: string }) {
  // Lexing is the expensive half and the transcript re-renders on every
  // keystroke in the composer, so the token tree is cut once per body of text.
  const tokens = useMemo(() => {
    try {
      return Lexer.lex(children, { gfm: true });
    } catch {
      return null;
    }
  }, [children]);

  // A body that will not parse is still a body worth reading.
  if (!tokens) return <Type size="body" color="body">{children}</Type>;
  return <View style={{ minWidth: 0 }}><Blocks tokens={tokens} /></View>;
});

const mono = { ios: "Menlo", android: "monospace", default: "monospace" } as const;

const makeStyles = (color: Palette) => StyleSheet.create({
  paragraph: { marginVertical: space.tight },
  heading: { marginTop: space.base, marginBottom: space.tight },
  strong: { fontWeight: "600", color: color.ink },
  em: { fontStyle: "italic" },
  del: { textDecorationLine: "line-through", color: color.inkMuted },
  codespan: {
    fontFamily: mono.default,
    fontSize: textSize.note,
    color: color.ink,
    backgroundColor: color.sunken,
  },
  link: { color: color.secondary, textDecorationLine: "underline" },
  listRow: { flexDirection: "row", gap: space.snug, alignItems: "flex-start" },
  marker: { color: color.inkMuted, fontSize: textSize.body, lineHeight: Math.round(textSize.body * 1.45), minWidth: 16 },
  quote: {
    borderLeftWidth: 2,
    borderLeftColor: color.secondaryLine,
    paddingLeft: space.base,
    marginVertical: space.snug,
  },
  pre: {
    marginVertical: space.base,
    borderRadius: radius.control,
    backgroundColor: color.ink,
    paddingHorizontal: space.base,
    paddingVertical: space.snug,
  },
  preText: { fontFamily: mono.default, fontSize: textSize.note, color: color.surface, lineHeight: 18 },
  table: {
    marginVertical: space.base,
    borderRadius: radius.control,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: color.line,
  },
  tableRow: { flexDirection: "row", borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: color.line },
  tableHead: { backgroundColor: color.sunken },
  tableCell: { paddingHorizontal: space.base, paddingVertical: space.snug, minWidth: 110, justifyContent: "center" },
  tableCellLast: { alignItems: "flex-end" },
  rule: { height: StyleSheet.hairlineWidth, backgroundColor: color.line, marginVertical: space.base },
});
