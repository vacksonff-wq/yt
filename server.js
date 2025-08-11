const express = require("express");
const cors = require("cors");
const { exec } = require("child_process");

const app = express();
app.use(cors());
app.use(express.json());

app.get("/", (req, res) => {
  res.send("YouTube Downloader API is running ✅");
});

app.get("/download", (req, res) => {
  const videoUrl = req.query.url;
  if (!videoUrl) return res.status(400).json({ error: "URL is required" });

  exec(`yt-dlp -f best -g "${videoUrl}"`, (err, stdout, stderr) => {
    if (err) {
      console.error("yt-dlp error:", stderr);
      return res.status(500).json({ error: "yt-dlp execution failed", detail: stderr });
    }
    if (!stdout.trim()) {
      console.error("No URL returned by yt-dlp:", stderr);
      return res.status(500).json({ error: "No download URL extracted", detail: stderr });
    }
    res.json({ direct_url: stdout.trim() });
  });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});
