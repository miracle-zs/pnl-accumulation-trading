const primaryFontStack = [
  '"Barlow"',
  "-apple-system",
  "BlinkMacSystemFont",
  '"PingFang SC"',
  '"Hiragino Sans GB"',
  '"Noto Sans SC"',
  '"Microsoft YaHei"',
  '"Segoe UI"',
  "sans-serif",
];

module.exports = {
  content: [
    "../src/pnl_trader/web/static/index.html",
    "../src/pnl_trader/web/static/js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        shell: {
          bg: "var(--shell-bg)",
          surface: "var(--shell-surface)",
          "surface-hover": "var(--shell-surface-hover)",
          border: "var(--shell-border)",
          text: "var(--shell-text)",
          muted: "var(--shell-muted)",
          beacon: "var(--shell-beacon)",
        },
        canvas: "var(--bg)",
        surface: {
          DEFAULT: "var(--surface)",
          subtle: "var(--surface-subtle)",
          hover: "var(--surface-hover)",
        },
        border: {
          DEFAULT: "var(--border)",
          strong: "var(--border-strong)",
        },
        ink: {
          DEFAULT: "var(--ink)",
          secondary: "var(--ink-secondary)",
          muted: "var(--ink-muted)",
        },
        accent: {
          DEFAULT: "var(--accent)",
          hover: "var(--accent-hover)",
          soft: "var(--accent-soft)",
        },
        gain: {
          DEFAULT: "var(--gain)",
          bg: "var(--gain-bg)",
          border: "var(--gain-border)",
        },
        loss: {
          DEFAULT: "var(--loss)",
          bg: "var(--loss-bg)",
          border: "var(--loss-border)",
        },
        warn: {
          DEFAULT: "var(--warn)",
          bg: "var(--warn-bg)",
          border: "var(--warn-border)",
        },
        tier: {
          t1: "var(--tier-t1)",
          "t1-bar": "var(--tier-t1-bar)",
          "t1-bg": "var(--tier-t1-bg)",
          "t1-border": "var(--tier-t1-border)",
          t2: "var(--tier-t2)",
          "t2-bar": "var(--tier-t2-bar)",
          "t2-bg": "var(--tier-t2-bg)",
          "t2-border": "var(--tier-t2-border)",
          t21: "var(--tier-t21)",
          "t21-bg": "var(--tier-t21-bg)",
          "t21-border": "var(--tier-t21-border)",
          t3: "var(--tier-t3)",
          "t3-bar": "var(--tier-t3-bar)",
          "t3-bg": "var(--tier-t3-bg)",
          "t3-border": "var(--tier-t3-border)",
          t31: "var(--tier-t31)",
          "t31-bar": "var(--tier-t31-bar)",
          "t31-bg": "var(--tier-t31-bg)",
          "t31-border": "var(--tier-t31-border)",
        },
      },
      fontFamily: {
        display: primaryFontStack,
        sans: primaryFontStack,
        mono: [
          '"SF Mono"',
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};
