import axios from "axios";

export async function submitCheckout(orderId: string) {
  return axios.post("/api/checkout", { orderId });
}
