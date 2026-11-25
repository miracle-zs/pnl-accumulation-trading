module.exports = {
  content: [
    "../src/pnl_trader/web/static/index.html",
    "../src/pnl_trader/web/static/js/**/*.js",
  ],
  theme: {
    extend: {
      fontFamily: {
        display: [
          '"Barlow"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Noto Sans SC"',
          "-apple-system",
          "sans-serif",
        ],
        sans: [
          '"Barlow"',
          "-apple-system",
          "BlinkMacSystemFont",
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Noto Sans SC"',
          '"Segoe UI"',
          "sans-serif",
        ],
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
