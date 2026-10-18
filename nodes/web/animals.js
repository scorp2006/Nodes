// Cute animal avatars for agents: small inline SVG faces (no emoji, so they look the same everywhere), tinted with the
// agent's colour. Agents get animals and colours in order of appearance, so several subagents never look alike.
export const PALETTE = ["#ff8a7a", "#5ab4ff", "#ffd166", "#4cd4a0", "#ffa94d", "#3bc9db", "#a9e34b", "#ff8fab", "#e9c46a", "#74c0fc"];
export const ANIMALS = ["fox", "cat", "bear", "panda", "owl", "rabbit", "penguin", "frog"];

const order = [];
export const slot = name => { let i = order.indexOf(name); if (i < 0) { order.push(name); i = order.length - 1; } return i; };
export const colorFor = name => PALETTE[slot(name) % PALETTE.length];
export const animalFor = name => ANIMALS[slot(name) % ANIMALS.length];

const INK = "#1d1d22", CHEEK = "#ff9eb0";
const eyes = (y = 25, dx = 6.5, r = 2.6) => `<circle cx="${22 - dx}" cy="${y}" r="${r}" fill="${INK}"/><circle cx="${22 + dx}" cy="${y}" r="${r}" fill="${INK}"/>
  <circle cx="${22 - dx + .9}" cy="${y - .9}" r=".9" fill="#fff"/><circle cx="${22 + dx + .9}" cy="${y - .9}" r=".9" fill="#fff"/>`;
const cheeks = (y = 30) => `<circle cx="12.5" cy="${y}" r="2.6" fill="${CHEEK}" opacity=".7"/><circle cx="31.5" cy="${y}" r="2.6" fill="${CHEEK}" opacity=".7"/>`;
const smile = (y = 31) => `<path d="M19.5 ${y} q2.5 2.4 5 0" stroke="${INK}" stroke-width="1.4" fill="none" stroke-linecap="round"/>`;

const FACES = {
  fox: c => `<path d="M7 9 L14 21 L9 24Z M37 9 L30 21 L35 24Z" fill="${c}"/><path d="M9.5 12 L13 19.5 L10.5 21Z M34.5 12 L31 19.5 L33.5 21Z" fill="#fff" opacity=".6"/>
    <path d="M8 24 Q22 8 36 24 Q36 37 22 40 Q8 37 8 24Z" fill="${c}"/><path d="M13 30 Q22 41 31 30 Q28 39 22 40 Q16 39 13 30Z" fill="#fff"/>
    ${eyes(25)}<ellipse cx="22" cy="31.5" rx="2" ry="1.5" fill="${INK}"/>`,
  cat: c => `<path d="M9 10 L17 18 L10 22Z M35 10 L27 18 L34 22Z" fill="${c}"/><path d="M11 13 L15 17.5 L11.5 19.5Z M33 13 L29 17.5 L32.5 19.5Z" fill="${CHEEK}"/>
    <circle cx="22" cy="26" r="14" fill="${c}"/>${eyes(25)}${cheeks(30)}<path d="M21 29.5 h2 l-1 1.3z" fill="${INK}"/>${smile(31.5)}
    <path d="M5 27 h7 M5 30.5 h7 M32 27 h7 M32 30.5 h7" stroke="${INK}" stroke-width=".8" opacity=".5"/>`,
  bear: c => `<circle cx="11" cy="13" r="5.5" fill="${c}"/><circle cx="33" cy="13" r="5.5" fill="${c}"/><circle cx="11" cy="13" r="2.6" fill="${CHEEK}" opacity=".8"/>
    <circle cx="33" cy="13" r="2.6" fill="${CHEEK}" opacity=".8"/><circle cx="22" cy="25" r="15" fill="${c}"/>${eyes(23.5)}
    <ellipse cx="22" cy="30" rx="6" ry="4.6" fill="#fff" opacity=".75"/><ellipse cx="22" cy="28.4" rx="2.2" ry="1.6" fill="${INK}"/>${smile(30.5)}`,
  panda: c => `<circle cx="11" cy="12" r="5.5" fill="${INK}"/><circle cx="33" cy="12" r="5.5" fill="${INK}"/><circle cx="22" cy="25" r="15" fill="#fff"/>
    <ellipse cx="15.5" cy="24" rx="4" ry="5" fill="${INK}" transform="rotate(-20 15.5 24)"/><ellipse cx="28.5" cy="24" rx="4" ry="5" fill="${INK}" transform="rotate(20 28.5 24)"/>
    <circle cx="15.5" cy="24" r="1.7" fill="#fff"/><circle cx="28.5" cy="24" r="1.7" fill="#fff"/><ellipse cx="22" cy="30" rx="2.2" ry="1.5" fill="${INK}"/>
    ${smile(31.6)}<path d="M8 38 Q22 46 36 38" stroke="${c}" stroke-width="4" fill="none" stroke-linecap="round"/>`,
  owl: c => `<path d="M9 9 L15 16 L9 19Z M35 9 L29 16 L35 19Z" fill="${c}"/><ellipse cx="22" cy="25" rx="15" ry="15.5" fill="${c}"/>
    <circle cx="15.5" cy="23" r="6" fill="#fff"/><circle cx="28.5" cy="23" r="6" fill="#fff"/><circle cx="15.5" cy="23" r="3" fill="${INK}"/>
    <circle cx="28.5" cy="23" r="3" fill="${INK}"/><circle cx="16.4" cy="22" r="1" fill="#fff"/><circle cx="29.4" cy="22" r="1" fill="#fff"/>
    <path d="M20 29 L22 33 L24 29Z" fill="#ffb347"/><path d="M14 35 q2 2 4 0 q2 2 4 0 q2 2 4 0 q2 2 4 0" stroke="#fff" stroke-width="1.2" fill="none" opacity=".6"/>`,
  rabbit: c => `<ellipse cx="15" cy="9" rx="4" ry="9.5" fill="${c}"/><ellipse cx="29" cy="9" rx="4" ry="9.5" fill="${c}"/><ellipse cx="15" cy="9.5" rx="1.8" ry="6.5" fill="${CHEEK}"/>
    <ellipse cx="29" cy="9.5" rx="1.8" ry="6.5" fill="${CHEEK}"/><circle cx="22" cy="27" r="13.5" fill="${c}"/>${eyes(26)}${cheeks(31)}
    <path d="M21 30 h2 l-1 1.2z" fill="${INK}"/><path d="M22 31.2 v1.6 M22 32.8 q-1.6 1.4 -3 .4 M22 32.8 q1.6 1.4 3 .4" stroke="${INK}" stroke-width="1.1" fill="none" stroke-linecap="round"/>`,
  penguin: c => `<ellipse cx="22" cy="24" rx="15" ry="16" fill="#46505f"/><path d="M12 25 Q12 15 22 17 Q32 15 32 25 Q32 37 22 38 Q12 37 12 25Z" fill="#fff"/>
    ${eyes(24, 5, 2.3)}<path d="M19 28.5 L22 32 L25 28.5Z" fill="#ffb347"/>${cheeks(30.5)}
    <path d="M9 38 Q22 44 35 38" stroke="${c}" stroke-width="5" fill="none" stroke-linecap="round"/>`,
  frog: c => `<circle cx="13.5" cy="13.5" r="6.5" fill="${c}"/><circle cx="30.5" cy="13.5" r="6.5" fill="${c}"/><circle cx="13.5" cy="13.5" r="4" fill="#fff"/>
    <circle cx="30.5" cy="13.5" r="4" fill="#fff"/><circle cx="13.5" cy="14" r="2.2" fill="${INK}"/><circle cx="30.5" cy="14" r="2.2" fill="${INK}"/>
    <ellipse cx="22" cy="27" rx="16" ry="12.5" fill="${c}"/>${cheeks(29)}<path d="M14 29 Q22 36 30 29" stroke="${INK}" stroke-width="1.6" fill="none" stroke-linecap="round"/>`,
};

export function animalSVG(name, size = 44) {
  const c = colorFor(name);
  return `<svg viewBox="0 0 44 44" width="${size}" height="${size}" aria-hidden="true">${FACES[animalFor(name)](c)}</svg>`;
}
