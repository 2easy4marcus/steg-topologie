import fs from "node:fs";

const input = "build/.postman-generated.json";
const generated = JSON.parse(fs.readFileSync(input, "utf8"));

function requests(items) {
  return items.flatMap((item) => item.request ? [item] : requests(item.item || []));
}

const publicItems = requests(generated.item).map((item) => {
  const path = `/${item.request.url.path.join("/")}`;
  return {
    name: item.name,
    request: {
      method: item.request.method,
      header: item.request.header,
      url: `{{baseUrl}}${path}`,
    },
    event: [{
      listen: "test",
      script: {
        type: "text/javascript",
        exec: [
          "pm.test(\"returns 200\", function () {",
          "  pm.response.to.have.status(200);",
          "});",
        ],
      },
    }],
  };
}).sort((a, b) => a.request.url.localeCompare(b.request.url));

const schema = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json";
const publicCollection = {
  info: {name: "Tunisia Outage Tracker Public Smoke", schema},
  item: publicItems,
};
const securityCollection = {
  info: {name: "Tunisia Outage Tracker Security Smoke", schema},
  item: [{
    name: "Internal operations rejects missing secret",
    request: {
      method: "GET",
      header: [],
      url: "{{baseUrl}}/api/internal/ops/summary",
    },
    event: [{
      listen: "test",
      script: {
        type: "text/javascript",
        exec: [
          "pm.test(\"rejects missing operations secret\", function () {",
          "  pm.response.to.have.status(401);",
          "});",
        ],
      },
    }],
  }],
};
const environment = {
  name: "Tunisia Outage Tracker local",
  values: [
    {key: "baseUrl", value: "http://app:8010", enabled: true},
    {key: "opsSecret", value: "", enabled: true},
  ],
};

fs.mkdirSync("postman", {recursive: true});
for (const [path, value] of [
  ["postman/tunisia-outage-tracker.postman_collection.json", publicCollection],
  ["postman/tunisia-outage-tracker-security-smoke.postman_collection.json", securityCollection],
  ["postman/environment.example.json", environment],
]) {
  fs.writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`);
}
fs.unlinkSync(input);
