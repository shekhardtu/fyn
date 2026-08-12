import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The React Compiler was enabled here and taken back out.
  //
  // It did what it promises: with the memo boundaries in place a keystroke went
  // from 4.6ms to 1.8ms in dev and from 1.2ms to 0.7ms in a production build.
  // But both of those already sit far inside a single 16.7ms frame, so none of
  // the difference is perceptible — while the browser suite went from passing
  // to failing two runs in three. The header's "Conversation link copied" state
  // stopped being observable after a click on a long thread; the same click
  // works when it is made a few seconds later, so it reads as a timing problem
  // around hydration rather than a miscompilation. That is too fragile to ship
  // for a win nobody can see.
  //
  // Worth revisiting as `reactCompiler: { compilationMode: "annotation" }`,
  // which opts in one component at a time with a "use memo" directive, once
  // that interaction is understood.
};

export default nextConfig;
