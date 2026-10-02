"use client";

import { useEffect, useRef, useState } from "react";

/**
 * The page's motion vocabulary: one easing, numbers that count, and
 * "play once when first seen".
 * Every piece checks prefers-reduced-motion and lands on the final state.
 */

export const EASE_OUT = (t: number) => 1 - Math.pow(1 - t, 4);

export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** True once the element has scrolled into view; never goes back to false. */
export function useInView<T extends Element>(): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || seen) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setSeen(true);
          io.disconnect();
        }
      },
      { rootMargin: "0px 0px -10% 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [seen]);
  return [ref, seen];
}

const NUMBER = /-?\d[\d,]*(\.\d+)?/;

/**
 * Counts the first number in `text` up from zero when first seen, and from
 * its last value on every later change. "$1,204.50", "93%", "−$4.10" all
 * keep their prefix, suffix, separators and decimals. Text with no number
 * ("—") renders as is. Screen readers get the final text only.
 */
export function CountUp({ text, duration = 1400 }: { text: string; duration?: number }) {
  const [ref, seen] = useInView<HTMLSpanElement>();
  const match = NUMBER.exec(text);
  const target = match ? Number(match[0].replace(/,/g, "")) : null;
  const decimals = match?.[1] ? match[1].length - 1 : 0;
  const from = useRef(0);
  const [shown, setShown] = useState<number | null>(null);

  useEffect(() => {
    if (target === null || !seen) return;
    if (prefersReducedMotion()) {
      setShown(target);
      from.current = target;
      return;
    }
    const start = performance.now();
    const origin = from.current;
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      setShown(origin + (target - origin) * EASE_OUT(t));
      if (t < 1) frame = requestAnimationFrame(step);
      else from.current = target;
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, seen, duration]);

  if (!match || target === null) return <span ref={ref}>{text}</span>;

  const value = shown ?? (seen ? target : 0);
  const body = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
    useGrouping: match[0].includes(","),
  });
  const animated =
    text.slice(0, match.index) + (match[0].startsWith("-") ? "-" : "") + body + text.slice(match.index + match[0].length);

  return (
    <span ref={ref} className="countup">
      <span aria-hidden="true">{shown === null && !seen ? text.replace(NUMBER, (m) => m.replace(/\d/g, "0")) : animated}</span>
      <span className="sr-only">{text}</span>
    </span>
  );
}
