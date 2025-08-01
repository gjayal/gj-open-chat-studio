/* eslint-disable */
/* tslint:disable */
/**
 * This is an autogenerated file created by the Stencil compiler.
 * It contains typing information for all components that exist in this project.
 */
import { HTMLStencilElement, JSXBase } from "@stencil/core/internal";
export namespace Components {
    interface OpenChatStudioWidget {
        /**
          * The base URL for the API (defaults to current origin).
         */
        "apiBaseUrl"?: string;
        /**
          * The shape of the chat button. 'round' makes it circular, 'square' keeps it rectangular.
         */
        "buttonShape": 'round' | 'square';
        /**
          * The text to display on the button.
         */
        "buttonText"?: string;
        /**
          * The ID of the chatbot to connect to.
         */
        "chatbotId": string;
        /**
          * Whether the chat widget is initially expanded.
         */
        "expanded": boolean;
        /**
          * URL of the icon to display on the button. If not provided, uses the default OCS logo.
         */
        "iconUrl"?: string;
        /**
          * The initial position of the chat widget on the screen.
         */
        "position": 'left' | 'center' | 'right';
        /**
          * Array of starter questions that users can click to send (JSON array of strings)
         */
        "starterQuestions"?: string;
        /**
          * Whether the chat widget is visible on load.
         */
        "visible": boolean;
        /**
          * Welcome messages to display above starter questions (JSON array of strings)
         */
        "welcomeMessages"?: string;
    }
}
declare global {
    interface HTMLOpenChatStudioWidgetElement extends Components.OpenChatStudioWidget, HTMLStencilElement {
    }
    var HTMLOpenChatStudioWidgetElement: {
        prototype: HTMLOpenChatStudioWidgetElement;
        new (): HTMLOpenChatStudioWidgetElement;
    };
    interface HTMLElementTagNameMap {
        "open-chat-studio-widget": HTMLOpenChatStudioWidgetElement;
    }
}
declare namespace LocalJSX {
    interface OpenChatStudioWidget {
        /**
          * The base URL for the API (defaults to current origin).
         */
        "apiBaseUrl"?: string;
        /**
          * The shape of the chat button. 'round' makes it circular, 'square' keeps it rectangular.
         */
        "buttonShape"?: 'round' | 'square';
        /**
          * The text to display on the button.
         */
        "buttonText"?: string;
        /**
          * The ID of the chatbot to connect to.
         */
        "chatbotId": string;
        /**
          * Whether the chat widget is initially expanded.
         */
        "expanded"?: boolean;
        /**
          * URL of the icon to display on the button. If not provided, uses the default OCS logo.
         */
        "iconUrl"?: string;
        /**
          * The initial position of the chat widget on the screen.
         */
        "position"?: 'left' | 'center' | 'right';
        /**
          * Array of starter questions that users can click to send (JSON array of strings)
         */
        "starterQuestions"?: string;
        /**
          * Whether the chat widget is visible on load.
         */
        "visible"?: boolean;
        /**
          * Welcome messages to display above starter questions (JSON array of strings)
         */
        "welcomeMessages"?: string;
    }
    interface IntrinsicElements {
        "open-chat-studio-widget": OpenChatStudioWidget;
    }
}
export { LocalJSX as JSX };
declare module "@stencil/core" {
    export namespace JSX {
        interface IntrinsicElements {
            "open-chat-studio-widget": LocalJSX.OpenChatStudioWidget & JSXBase.HTMLAttributes<HTMLOpenChatStudioWidgetElement>;
        }
    }
}
