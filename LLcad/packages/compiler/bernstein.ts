/** Exact rational Bernstein arithmetic: decimal inputs never pass through float. */
export type Q = { n: bigint; d: bigint };
export type Poly = Q[];
const gcd = (a: bigint, b: bigint): bigint => {
  a = a < 0n ? -a : a;
  b = b < 0n ? -b : b;
  while (b) [a, b] = [b, a % b];
  return a || 1n;
};
export function fraction(n: bigint, d = 1n): Q {
  if (!d) throw new Error("Zero rational denominator");
  if (d < 0n) {
    n = -n;
    d = -d;
  }
  const g = gcd(n, d);
  return { n: n / g, d: d / g };
}
export function decimal(s: string): Q {
  const [whole, digits = ""] = s.split(".");
  return fraction(BigInt(whole + digits), 10n ** BigInt(digits.length));
}
export const zero = fraction(0n),
  one = fraction(1n);
export const add = (a: Q, b: Q) => fraction(a.n * b.d + b.n * a.d, a.d * b.d);
export const neg = (a: Q) => ({ n: -a.n, d: a.d });
export const sub = (a: Q, b: Q) => add(a, neg(b));
export const mul = (a: Q, b: Q) => fraction(a.n * b.n, a.d * b.d);
export const div = (a: Q, b: Q) => fraction(a.n * b.d, a.d * b.n);
export const abs = (a: Q) => ({ n: a.n < 0n ? -a.n : a.n, d: a.d });
export const cmp = (a: Q, b: Q) =>
  a.n * b.d < b.n * a.d ? -1 : a.n * b.d > b.n * a.d ? 1 : 0;
export const min = (a: Q, b: Q) => (cmp(a, b) <= 0 ? a : b);
export const max = (a: Q, b: Q) => (cmp(a, b) >= 0 ? a : b);
const choose = (n: number, k: number) => {
  let value = 1n;
  for (let i = 1; i <= k; i++) value = (value * BigInt(n - i + 1)) / BigInt(i);
  return value;
};
export function elevate(a: Poly, degree: number): Poly {
  while (a.length <= degree) {
    const n = a.length;
    a = Array.from({ length: n + 1 }, (_, i) =>
      add(
        mul(a[i - 1] ?? zero, fraction(BigInt(i), BigInt(n))),
        mul(a[i] ?? zero, fraction(BigInt(n - i), BigInt(n))),
      ),
    );
  }
  return a;
}
export function plus(a: Poly, b: Poly): Poly {
  const n = Math.max(a.length, b.length) - 1;
  const other = elevate(b, n);
  return elevate(a, n).map((x, i) => add(x, other[i]));
}
export const times = (a: Poly, c: Q) => a.map((x) => mul(x, c));
export const minus = (a: Poly, b: Poly) => plus(a, times(b, fraction(-1n)));
export function product(a: Poly, b: Poly): Poly {
  const m = a.length - 1,
    n = b.length - 1;
  return Array.from({ length: m + n + 1 }, (_, k) => {
    let coefficient = zero;
    for (let i = Math.max(0, k - n); i <= Math.min(m, k); i++) {
      const weight = fraction(
        choose(m, i) * choose(n, k - i),
        choose(m + n, k),
      );
      coefficient = add(coefficient, mul(mul(a[i], b[k - i]), weight));
    }
    return coefficient;
  });
}
export const derivative = (a: Poly) =>
  a.length === 1
    ? [zero]
    : a
        .slice(1)
        .map((x, i) => mul(sub(x, a[i]), fraction(BigInt(a.length - 1))));
export const power = (a: Poly, n: number): Poly =>
  n === 0 ? [one] : product(a, power(a, n - 1));
export const bound = (a: Poly) => a.map(abs).reduce(max, zero);
function format(n: bigint, digits: number) {
  if (!n) return "0";
  const text = n.toString().padStart(digits + 1, "0");
  return (text.slice(0, -digits) + "." + text.slice(-digits))
    .replace(/0+$/, "")
    .replace(/\.$/, "");
}
/** Positive rational rounded outward to preserve the proved bound. */
export function upper(a: Q, digits = 30): string {
  return format((a.n * 10n ** BigInt(digits) + a.d - 1n) / a.d, digits);
}
export function lower(a: Q, digits = 30): string {
  return format((a.n * 10n ** BigInt(digits)) / a.d, digits);
}
