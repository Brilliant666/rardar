import { networkInterfaces } from "node:os";

const interfaces = networkInterfaces();
if (interfaces === null || typeof interfaces !== "object" || Array.isArray(interfaces)) {
  throw new TypeError("os.networkInterfaces() did not return an object");
}

console.log("AF_NETLINK_PROBE_OK");
