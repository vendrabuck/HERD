import { BrowserRouter, Routes } from "react-router-dom";
import { Toaster } from "react-hot-toast";
import { ErrorBoundary } from "@/components/ui/ErrorBoundary";
import { appRouteElements } from "@/routes";

export default function App() {
  return (
    <BrowserRouter>
      <Toaster position="bottom-center" />
      <ErrorBoundary>
        <Routes>{appRouteElements}</Routes>
      </ErrorBoundary>
    </BrowserRouter>
  );
}
