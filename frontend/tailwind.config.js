/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        mesh: { navy: "#02042B", panel: "#080E29", blue: "#1863D6", mint: "#00D09C" },
      },
    },
  },
  plugins: [],
};
