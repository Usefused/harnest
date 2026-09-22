package agentpack

import (
	"bytes"
	"debug/macho"
	"fmt"
	"os"
)

// extendMachO places the payload inside __LINKEDIT so macOS can sign the complete executable.
func extendMachO(file *os.File) error {
	image, err := macho.NewFile(file)
	if err != nil {
		return nil
	} // ELF, PE and test fixtures use ordinary appended ZIPs.
	info, err := file.Stat()
	if err != nil {
		return err
	}
	offset := int64(32)
	for _, load := range image.Loads {
		raw := load.Raw()
		segment, ok := load.(*macho.Segment)
		if ok && segment.Name == "__LINKEDIT" {
			size := uint64(info.Size()) - segment.Offset
			image.ByteOrder.PutUint64(raw[32:40], (size+16383)&^16383)
			image.ByteOrder.PutUint64(raw[48:56], size)
			_, err = file.WriteAt(raw, offset)
			return err
		}
		offset += int64(len(raw))
	}
	return fmt.Errorf("Mach-O launcher has no __LINKEDIT segment")
}

// executablePayloadEnd excludes macOS signatures, which can exceed ZIP's end-search window.
func executablePayloadEnd(file *os.File) (int64, error) {
	info, err := file.Stat()
	if err != nil {
		return 0, err
	}
	image, err := macho.NewFile(file)
	if err != nil {
		return info.Size(), nil
	}
	for _, load := range image.Loads {
		raw := load.Raw()
		if len(raw) >= 16 && image.ByteOrder.Uint32(raw[:4]) == 0x1d {
			return int64(image.ByteOrder.Uint32(raw[8:12])), nil
		}
	}
	return info.Size(), nil
}

// HasMachOSignature distinguishes signed arm64 stubs from unsigned Intel macOS stubs.
func HasMachOSignature(data []byte) bool {
	image, err := macho.NewFile(bytes.NewReader(data))
	if err != nil {
		return false
	}
	for _, load := range image.Loads {
		raw := load.Raw()
		if len(raw) >= 16 && image.ByteOrder.Uint32(raw[:4]) == 0x1d {
			return true
		}
	}
	return false
}
